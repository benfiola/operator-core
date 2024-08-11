import asyncio
import contextlib
import functools
import inspect
import logging
import os
import pathlib
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    ClassVar,
    Coroutine,
    Generic,
    Protocol,
    TypedDict,
    TypeVar,
    cast,
)

import fastapi
import kopf
import kopf._cogs.structs.diffs
import lightkube.config.kubeconfig
import lightkube.core.async_client
import lightkube.core.resource
import lightkube.core.resource_registry
import lightkube.generic_resource
import lightkube.models.meta_v1
import pydantic
import uvicorn


class OperatorError(Exception):
    """
    Defines a custom exception raised by this module.

    Used primarily to help identify known errors for proper error management.

    (NOTE: see `handle_hook_exception`)
    """

    recoverable: bool

    def __init__(self, message: str, recoverable: bool = False):
        super().__init__(message)
        self.recoverable = recoverable


ResourceSpec = TypeVar("ResourceSpec")


class ResourceStatus(pydantic.BaseModel, Generic[ResourceSpec]):
    """
    A generic data container for resource statuses.
    """

    # the currently applied spec for the resource (can differ from the resource 'spec' when an invalid edit is made)
    currentSpec: ResourceSpec | None = None


class ResourceMeta(TypedDict):
    """
    Defines required metadata when creating a custom resource class
    """

    api_version: str
    kind: str
    plural: str


class BaseResource(pydantic.BaseModel, Generic[ResourceSpec]):
    """
    Provides custom fields and functionality between both GlobalResource and NamespacedResource classes.
    """

    apiVersion: str = ""
    kind: str = ""
    metadata: lightkube.models.meta_v1.ObjectMeta
    spec: ResourceSpec
    status: ResourceStatus[ResourceSpec] = pydantic.Field(
        default_factory=lambda: ResourceStatus()
    )

    # NOTE: required for lightkube
    _api_info: ClassVar[lightkube.core.resource.ApiInfo] = cast(
        lightkube.core.resource.ApiInfo, None
    )

    # operator-core specific internal fields
    # NOTE: dunder attributes prevent pydantic from processing them
    __oc_bases__: set[type] = set()
    __oc_resource__: ResourceMeta = cast(ResourceMeta, None)
    __oc_immutable_fields__: set[tuple[str]] = cast(set[tuple[str]], None)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        cls.__oc_bases__ = set(cls.__oc_bases__)
        if len(cls.__oc_bases__) < 2:
            # ignore base classes (NamespacedResource, GlobalResource)
            # ignore pydantic anonymous subclasses (NamespacedResource[ServiceSpec], etc.)
            cls.__oc_bases__.add(cls)
            return

        if cls.__oc_resource__ is None:
            # resource *must* define metadata
            raise TypeError(f"{cls} must set __oc_resource__ field")

        # ensure immutable fields is a set (and not 'None')
        # ensure every class has its own copy of the immutable fields set
        immutable_fields = cls.__oc_immutable_fields__ or set()
        cls.__oc_immutable_fields__ = set(immutable_fields)

        api_version = cls.__oc_resource__["api_version"]
        api_version_parts = api_version.split("/")
        if len(api_version_parts) == 1:
            api_version_parts = "", *api_version_parts
        group, version = api_version_parts
        kind = cls.__oc_resource__["kind"]
        plural = cls.__oc_resource__["plural"]

        # create required lightkube internal field
        cls._api_info = lightkube.generic_resource.create_api_info(
            group, version, kind, plural
        )

        # use metadata to set defaults for instance-level attributes
        cls.model_fields["apiVersion"].default = api_version
        cls.model_fields["kind"].default = kind

        # register model with lightkube
        lightkube.core.resource_registry.resource_registry.register(cast(Any, cls))

    @classmethod
    def from_dict(cls, v, **kwargs):
        """
        NOTE: required for lightkube
        """
        return cls.model_validate(v)

    def to_dict(self, **kwargs):
        """
        NOTE: required for lightkube
        """
        return self.model_dump()


class NamespacedResource(
    BaseResource[ResourceSpec], lightkube.core.resource.NamespacedResource
):
    """
    Convenience base-class for namespaced resources.
    """

    pass


class GlobalResource(
    BaseResource[ResourceSpec], lightkube.core.resource.GlobalResource
):
    """
    Convenience base-class for global resources.
    """

    pass


Resource = NamespacedResource | GlobalResource
SomeResource = TypeVar("SomeResource", bound=Resource, contravariant=True)


class ResourceCallback(Protocol[SomeResource]):
    async def __call__(
        self, resource: SomeResource, *, logger: kopf.Logger
    ) -> None: ...


class Operator:
    """
    Implements a base operator class on which concrete operators can be built.
    """

    # health fastapi instance
    health_fastapi: fastapi.FastAPI
    # port used for health endpoint server
    health_port: int
    # a client capable of communcating with kubernetes
    kube_client: lightkube.core.async_client.AsyncClient
    # an (optional) path to a kubeconfig file
    kube_config: pathlib.Path | None
    # logger instance
    logger: logging.Logger
    # event signalling that the operator is ready
    ready_event: asyncio.Event
    # a kopf.OperatorRegistry instance enabling this operator to *not* run in the module scope
    registry: kopf.OperatorRegistry

    def __init__(
        self,
        *,
        health_port: int | None = None,
        kube_config: pathlib.Path | None = None,
        logger: logging.Logger,
    ):
        self.health_fastapi = fastapi.FastAPI()
        self.health_port = health_port or 8888
        self.kube_client = cast(lightkube.core.async_client.AsyncClient, None)
        self.kube_config = kube_config
        self.logger = logger
        self.ready_event = asyncio.Event()
        self.registry = kopf.OperatorRegistry()

        on_login = self.wrap_with_event_context("login", self.login)
        on_startup = self.wrap_with_event_context("startup", self.startup)
        kopf.on.login(registry=self.registry)(cast(Any, on_login))
        kopf.on.startup(registry=self.registry)(on_startup)

        self.health_fastapi.add_api_route("/healthz", self.health, methods=["GET"])

    def watch_resource(
        self,
        resource_cls: type[SomeResource],
        *,
        on_create: ResourceCallback[SomeResource] | None = None,
        on_update: ResourceCallback[SomeResource] | None = None,
        on_delete: ResourceCallback[SomeResource] | None = None,
    ) -> None:
        """
        Watches the given resource class by registering event listeners
        on several kubernetes resource events.
        """
        group = resource_cls._api_info.resource.group
        version = resource_cls._api_info.resource.version
        plural = resource_cls._api_info.plural

        if on_create:
            _on_create = self.wrap_with_event_context(
                "create",
                functools.partial(
                    self.resource_create, resource_cls=resource_cls, callback=on_create
                ),
            )
            kopf.on.create(group, version, plural, registry=self.registry)(_on_create)
        if on_update:
            _on_update = self.wrap_with_event_context(
                "update",
                functools.partial(
                    self.resource_update, resource_cls=resource_cls, callback=on_update
                ),
            )
            kopf.on.update(group, version, plural, registry=self.registry)(_on_update)
        if on_delete:
            _on_delete = self.wrap_with_event_context(
                "delete",
                functools.partial(
                    self.resource_delete,
                    resource_cls=resource_cls,
                    callback=on_delete,
                ),
            )
            kopf.on.delete(group, version, plural, registry=self.registry)(_on_delete)

    @contextlib.asynccontextmanager
    async def log_events(
        self, event: str, body: kopf.Body | None = None
    ) -> AsyncGenerator[None, None]:
        """
        A context that logs the start/finish of operator events.
        """
        event_name = event
        if body:
            namespace = body["metadata"].get("namespace", "<cluster>")
            name = body["metadata"]["name"]
            event_name = f"{event_name}:{namespace}/{name}"

        try:
            self.logger.info(f"{event_name} started")
            yield
            self.logger.info(f"{event_name} completed")
        except Exception as e:
            if isinstance(e, kopf.TemporaryError):
                self.logger.error(f"{event_name} failed with retryable error: {e}")
            elif isinstance(e, kopf.PermanentError):
                self.logger.error(f"{event_name} failed with non-retryable error: {e}")
            else:
                # NOTE: assumes 'handle_event_exceptions' is called before this
                raise NotImplementedError(e)
            raise e

    @contextlib.asynccontextmanager
    async def handle_event_exceptions(self) -> AsyncGenerator[None, None]:
        """
        A context that catches, processes and re-raises processed exceptions
        """
        try:
            yield
        except Exception as base_exception:
            exception = self.wrap_exception(base_exception)
            raise exception from base_exception

    def wrap_with_event_context(
        self, event: str, callback: Callable[..., Coroutine[Any, Any, Any]]
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """
        Wraps an operator event handler in a sequence of general contexts (e.g., error handling, logging)
        """

        signature = inspect.signature(callback)

        @functools.wraps(callback)
        async def inner(*args, **kwargs):
            async with contextlib.AsyncExitStack() as exit_stack:
                body = kwargs.get("body")
                await exit_stack.enter_async_context(self.log_events(event, body=body))
                await exit_stack.enter_async_context(self.handle_event_exceptions())
                _kwargs = {}
                for key in signature.parameters.keys():
                    if key not in kwargs:
                        continue
                    _kwargs[key] = kwargs[key]
                return await callback(**_kwargs)

        return inner

    async def login(self):
        """
        Authenticates the operator with kubernetes
        """
        if self.kube_config:
            self.logger.debug(f"kopf login using kubeconfig: {self.kube_config}")
            env = os.environ
            try:
                os.environ = dict(os.environ)
                os.environ["KUBECONFIG"] = f"{self.kube_config}"
                return kopf.login_with_kubeconfig()
            finally:
                os.environ = env
        else:
            self.logger.debug(f"kopf login using in-cluster")
            return kopf.login_with_service_account()

    async def startup(self):
        """
        Initializes the operator
        """
        kube_config = None
        if self.kube_config:
            kube_config = lightkube.config.kubeconfig.KubeConfig.from_file(
                self.kube_config
            )
        # TODO: remove when https://github.com/gtsystem/lightkube/pull/67 is published
        kube_config = cast(lightkube.config.kubeconfig.KubeConfig, kube_config)
        self.kube_client = lightkube.core.async_client.AsyncClient(kube_config)

    async def resource_create(
        self,
        *,
        resource_cls: type[SomeResource],
        callback: ResourceCallback[SomeResource],
        body: kopf.Body,
        patch: kopf.Patch,
        logger: kopf.Logger,
    ):
        """
        Called when a watched kubernetes resource is created
        """
        model = resource_cls.model_validate(dict(body))
        await callback(model, logger=logger)
        patch.status["currentSpec"] = model.to_dict()["spec"]

    def apply_diff_item(self, data: dict, diff_item: kopf.DiffItem):
        """
        Applies a diff item to the data object.

        Used during `resource_update` to incrementally update a resource.
        """
        new_data = dict(data)
        operation, field, old_value, new_value = diff_item
        if operation == "change":
            curr = new_data
            # traverse object parent fields
            for f in field[:-1]:
                curr = data[f]
            # set final field value
            field = field[-1]
            curr[field] = new_value
        else:
            raise NotImplementedError()
        return new_data

    async def resource_update(
        self,
        *,
        resource_cls: type[SomeResource],
        callback: ResourceCallback[SomeResource],
        body: kopf.Body,
        patch: kopf.Patch,
        logger: kopf.Logger,
    ):
        """
        Called when a watched kubernetes resource is updated
        """
        if body["status"].get("currentSpec") is None:
            # retry resoure creation if previous attempts have failed
            return await self.resource_create(
                resource_cls=resource_cls,
                callback=callback,
                body=body,
                logger=logger,
                patch=patch,
            )

        # calculate the diff between the desired spec and the current spec
        current = dict(body["status"]["currentSpec"])
        desired = dict(body["spec"])
        diff_items = kopf._cogs.structs.diffs.diff(current, desired)

        # incrementally update the resource
        for diff_item in diff_items:
            if diff_item[1] in resource_cls.__oc_immutable_fields__:
                # do not attempt to mutate immutable fields
                logger.info(f"ignoring immutable field: {diff_item[1]}")
                continue
            # apply diff item to create an updated model
            current = self.apply_diff_item(current, diff_item)
            data = dict(body)
            data.update({"spec": current})
            model = resource_cls.model_validate(data)
            # perform the update
            await callback(model, logger=logger)
            # update status if update successful
            patch.status["currentSpec"] = current

    async def resource_delete(
        self,
        *,
        resource_cls: type[SomeResource],
        callback: ResourceCallback[SomeResource],
        body: kopf.Body,
        logger: kopf.Logger,
    ):
        """
        Called when a watched kubernetes resource is deleted
        """
        if body["status"].get("currentSpec") is None:
            return

        # build and validate resource with current spec
        current_spec = dict(body["status"]["currentSpec"])
        data = dict(body)
        data.update({"spec": current_spec})
        model = resource_cls.model_validate(data)

        try:
            await callback(model, logger=logger)
        except OperatorError as e:
            if e.recoverable is True:
                raise e
            # unblock resource deletion if error is non-recoverable
            logger.exception(f"ignoring non-recoverable error", exc_info=e)

    def wrap_exception(
        self, exception: Exception
    ) -> kopf.TemporaryError | kopf.PermanentError:
        """
        Wraps an exception in either a kopf.TemporaryError or kopf.PermamentError and returns it

        NOTE: See `handle
        """
        wrapped_exception = kopf.TemporaryError(str(exception))
        if isinstance(exception, OperatorError):
            if not exception.recoverable:
                wrapped_exception = kopf.PermanentError(str(exception))
        elif isinstance(exception, pydantic.ValidationError):
            wrapped_exception = kopf.PermanentError(str(exception))
        return wrapped_exception

    async def health(self, response: fastapi.Response) -> fastapi.Response:
        """
        Health check, can be overridden - but recommended to call `super().health(response)`.

        Will return 200 if operator is ready, otherwise returns 500.
        """
        # if the operator isn't ready, set the status code to 500
        if not self.ready_event.is_set():
            response.status_code = 500
        # set the status code to 200 if unset
        response.status_code = response.status_code or 200

        passed = response.status_code == 200
        self.logger.debug(f"health check called (passed: {passed})")

        return response

    async def run(self):
        """
        Runs the operator - and blocks until exit.
        """

        class ServerConfig(uvicorn.Config):
            def configure_logging(self) -> None:
                pass

        # create healthcheck server
        server = uvicorn.Server(
            ServerConfig(app=self.health_fastapi, host="0.0.0.0", port=self.health_port)
        )

        await asyncio.gather(
            kopf.operator(
                clusterwide=True, ready_flag=self.ready_event, registry=self.registry
            ),
            server._serve(),
        )


__all__ = [
    "GlobalResource",
    "NamespacedResource",
    "OperatorError",
    "Operator",
]
