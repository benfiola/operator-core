import asyncio
import base64
import functools
import inspect
import json
import logging
import os
import pathlib
from typing import Any, Callable, Iterable, Protocol, TypeVar, cast

import fastapi
import kopf
import kopf._cogs.structs.diffs
import kubernetes
import pydantic
import urllib3
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


WrappedFn = TypeVar("WrappedFn", bound=Callable)


class HookFn(Protocol[WrappedFn]):
    """
    A callable with kopf hook/event data embedded
    """

    _hook_fn: bool
    _hook_event: str
    _hook_args: tuple[Any, ...]
    _hook_kwargs: dict[str, Any]
    __call__: WrappedFn


def hook(event: str, *args, **kwargs):
    """
    A decorator that attaches kopf hook/event data to an operator instance function
    """

    def inner(f: WrappedFn) -> HookFn[WrappedFn]:
        if not inspect.iscoroutinefunction(f):
            # only support async functions
            raise NotImplementedError()

        f.__dict__["_hook_fn"] = True
        f.__dict__["_hook_event"] = event
        f.__dict__["_hook_args"] = args
        f.__dict__["_hook_kwargs"] = kwargs

        return cast(HookFn[WrappedFn], f)

    return inner


def iter_hooks(obj: object) -> Iterable[HookFn]:
    """
    Given an object, iterates over instance members and yields functions decorated with the `hook` decorator.
    """
    for attr in dir(obj):
        val = getattr(obj, attr)
        if not inspect.ismethod(val):
            # attr is *not* a bound method
            continue

        # search class hierarchy for *any* method wrapped with @hook
        # NOTE: this prevents subclasses from having to re-decorate overridden methods
        hook_fn: HookFn | None = None
        classes = [obj.__class__]
        while classes:
            cls = classes.pop()
            if cls == object:
                # prevent infinite loop (object.__bases__ == [object])
                continue

            # get unbound method for current class
            unbound_method = getattr(cls, attr, None)
            if not unbound_method:
                # continue search - current class does not implement method
                continue
            # bind the unbound method to obj
            method = unbound_method.__get__(obj)
            if not getattr(method, "_hook_fn", False):
                # continue search - current method not decorated with @hook
                classes.extend(cls.__bases__)
                continue

            # stop search - @hook decorated bound method found
            method = cast(HookFn, method)
            hook_fn = method
            break

        if not hook_fn:
            # ignore attr - @hook decorated method not found in entire class hierarchy
            continue

        if hook_fn != val:
            # instance method not decorated with @hook, parent method was
            # call @hook on instance method  using parent method data
            hook_fn = hook(hook_fn._hook_event, *hook_fn._hook_args, **hook_fn._hook_kwargs)(val)

        yield hook_fn


def resource_namespace(resource: dict | kopf.Body) -> str:
    """
    Gets the namespace attached to a resource - returns 'default' if unset.

    NOTE: Assumes 'resource' is namespaced.
    """
    return resource["metadata"].get("namespace", "default")


def resource_fqn(resource: dict | kopf.Body) -> str:
    """
    Returns a full-qualified name for a dict-like kubernetes resource.

    A 'fully-qualfied name' is <namespace>/<name>
    """
    namespace = resource_namespace(resource)
    name = resource["metadata"]["name"]
    return f"{namespace}/{name}"


def log_hook(logger: logging.Logger, hook: WrappedFn) -> WrappedFn:
    """
    Decorates a kopf hook/function and logs when the hook is called
    and when the hook succeeds/fails.

    Will additionally log a resource's fully-qualfiied name if found.

    Accepts a 'logger' that's ultimately used to log hook activity
    """
    if not inspect.iscoroutinefunction(hook):
        raise NotImplementedError()

    @functools.wraps(hook)
    async def inner(*args, **kwargs):
        hook_name = hook.__name__
        if body := kwargs.get("body"):
            hook_name = f"{hook_name}:{resource_fqn(body)}"

        logger.info(f"{hook_name} started")
        try:
            rv = await hook(*args, **kwargs)
            logger.info(f"{hook_name} completed")
            return rv
        except Exception as e:
            if isinstance(e, kopf.TemporaryError):
                logger.error(f"{hook_name} failed with retryable error: {e}")
            elif isinstance(e, kopf.PermanentError):
                logger.error(f"{hook_name} failed with non-retryable error: {e}")
            raise e

    return cast(WrappedFn, inner)


HandleHookExceptionCallback = Callable[[Exception], None]


def handle_hook_exception(
    callback: HandleHookExceptionCallback,
    hook: WrappedFn,
) -> WrappedFn:
    """
    kopf will retry any hooks that fail with an exception - unless a
    kopf.PermanentError is raised.

    This method decorates a kopf event/hook function and wraps known errors
    in `kopf.PermanentError` to prevent spurious retries.

    Accepts a 'callback' that allows 'Operator' subclasses to further customize exception handling.
    """
    if not inspect.iscoroutinefunction(hook):
        raise NotImplementedError()

    @functools.wraps(hook)
    async def inner(*args, **kwargs):
        try:
            return await hook(*args, **kwargs)
        except OperatorError as e:
            if e.recoverable:
                raise kopf.TemporaryError(str(e)) from e
            else:
                raise kopf.PermanentError(str(e)) from e
        except pydantic.ValidationError as e:
            raise kopf.PermanentError(str(e)) from e
        except Exception as e:
            callback(e)
            raise kopf.TemporaryError(str(e)) from e

    return cast(WrappedFn, inner)


RunSyncRV = TypeVar("RunSyncRV")


async def run_sync(f: Callable[[], RunSyncRV]) -> RunSyncRV:
    """
    Convenience method to run sync functions within a thread pool executor
    to avoid blocking the running asyncio event loop.
    """
    return await asyncio.get_running_loop().run_in_executor(None, f)


class BaseModel(pydantic.BaseModel):
    model_config = {"populate_by_name": True}

    def model_dump(self, **kwargs):
        """
        pydantic's default `model_dump` method will produce a `dict` that (sometimes) cannot
        be serialized via `json.dumps`.

        This method avoids this shortcoming by dumping the model to a string and loading
        the result via `json.loads`.
        """
        data_str = self.model_dump_json(**kwargs)
        return json.loads(data_str)

    def model_dump_json(self, **kwargs):
        """
        Calls the parent `model_dump_json` but sets different defaults.

        Sets `by_alias` to True by default - the operator is often serializing
        data to kubernetes in camelcase - represented by aliases in pydantic.
        """
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)


cluster_namespace: str = "__cluster__"


class ResourceTypeRef(BaseModel):
    """
    A resource type ref refers to a resource type - useful primarily for list operations
    """

    api_version: str = pydantic.Field(..., alias=str("apiVersion"))
    plural: str
    namespace: str

    @property
    def is_namespaced(self) -> bool:
        return self.namespace != cluster_namespace


class ResourceRef(ResourceTypeRef):
    """
    A resource ref refers to a resource reference - useful for resource-level kubernetes api interactions.
    """

    name: str

    @property
    def fqn(self) -> str:
        value = f"{self.name}"
        if self.is_namespaced:
            value = f"{self.namespace}:{value}"
        return value


class ResourceKeyRef(ResourceRef):
    """
    A resource key ref refers to a key within a resource reference
    """

    key: str


SomeModel = TypeVar("SomeModel", bound=BaseModel)


def get_diff(a: SomeModel, b: SomeModel) -> kopf.Diff:
    """
    Helper method to return a diff between two models of the same class.
    """
    return kopf._cogs.structs.diffs.diff(a.model_dump(), b.model_dump())


def apply_diff_item(model: SomeModel, item: kopf.DiffItem) -> SomeModel:
    """
    Applies a given diff item to an model - returning an updated
    copy of the model.
    """
    data = model.model_dump()
    operation, field, old_value, new_value = item
    if operation == "change":
        curr = data
        # traverse object parent fields
        for f in field[:-1]:
            curr = data[f]
        # set final field value
        field = field[-1]
        curr[field] = new_value
    else:
        raise NotImplementedError()
    return type(model).model_validate(data)


def filter_immutable_diff_items(
    diff: kopf.Diff, immutable: set[tuple[str, ...]], kopf_logger: logging.Logger
) -> Iterable[kopf.DiffItem]:
    """
    Most resources have fields that shouldn't change during updates - and will often
    need to filter out diff items that attempt to modify existing fields.

    This helper function will yield diff items that aren't part of the provided immutable
    fields set.
    """
    for item in diff:
        if item[1] in immutable:
            kopf_logger.info(f"ignoring immutable field: {item[1]}")
            continue
        yield item


class ServerConfig(uvicorn.Config):
    """
    Override of uvicorn.Config that disables default behavior.

    - Prevents uvicorn from overriding logging configurations
    """

    def configure_logging(self) -> None:
        """
        This method is overridden to prevent uvicorn from overriding logging configurations
        """
        pass


async def create_kube_client(
    kube_config: pathlib.Path | None = None,
) -> kubernetes.client.ApiClient:
    """
    Creates a kubernetes api client.

    Will default to in-cluster configuration unless a kube config path is provided.
    """
    config = kubernetes.client.Configuration()
    if kube_config:
        kubernetes.config.load_kube_config(config_file=f"{kube_config}", client_configuration=config)
    else:
        kubernetes.config.load_incluster_config(client_configuration=config)
    kube_client = kubernetes.client.ApiClient(config)
    return kube_client


def get_resource_type_url(resource_type_ref: ResourceTypeRef) -> str:
    """
    Gets a url given a resource type ref
    """
    api_version_parts = resource_type_ref.api_version.split("/")
    if len(api_version_parts) == 1:
        # core apis use the /api base
        url = f"/api/{resource_type_ref.api_version}"
    else:
        # everything else uses the /apis base
        url = f"/apis/{resource_type_ref.api_version}"

    if resource_type_ref.is_namespaced:
        url = f"{url}/namespaces/{resource_type_ref.namespace}"

    url = f"{url}/{resource_type_ref.plural}"

    return url


def get_resource_url(resource_ref: ResourceRef) -> str:
    """
    Gets a url given a resource ref
    """
    resource_type_ref = ResourceTypeRef(
        api_version=resource_ref.api_version,
        plural=resource_ref.plural,
        namespace=resource_ref.namespace,
    )
    return f"{get_resource_type_url(resource_type_ref)}/{resource_ref.name}"


async def get_resource(kube_client: kubernetes.client.ApiClient, resource_ref: ResourceRef) -> dict | None:
    """
    Gets a kubernetes resource.  Returns 'None' if the resource does not exist.
    """

    def inner():
        return cast(
            urllib3.response.HTTPResponse,
            kube_client.call_api(
                get_resource_url(resource_ref),
                "GET",
                _return_http_data_only=True,
                _preload_content=False,
            ),
        )

    try:
        response = await run_sync(inner)
    except kubernetes.client.ApiException as e:
        try:
            data = json.loads(cast(Any, e.body).decode("utf-8"))
            message = data["message"]
        except Exception:
            raise e
        if f'"{resource_ref.name}" not found' not in message:
            raise e
        return None

    data = json.loads(response.data.decode("utf-8"))
    return data


async def list_resources(kube_client: kubernetes.client.ApiClient, resource_type_ref: ResourceTypeRef) -> list[dict]:
    """
    Lists kubernetes resources
    """

    def inner():
        return cast(
            urllib3.response.HTTPResponse,
            kube_client.call_api(
                get_resource_type_url(resource_type_ref),
                "GET",
                _return_http_data_only=True,
                _preload_content=False,
            ),
        )

    response = await run_sync(inner)
    data = json.loads(response.data.decode("utf-8"))
    return data["items"]


async def delete_resource(kube_client: kubernetes.client.ApiClient, resource_ref: ResourceRef):
    """
    Deletes a kubernetes resource
    """

    def inner():
        return cast(
            urllib3.response.HTTPResponse,
            kube_client.call_api(
                get_resource_url(resource_ref),
                "DELETE",
                _return_http_data_only=True,
                _preload_content=False,
            ),
        )

    return await run_sync(inner)


async def create_resource(kube_client: kubernetes.client.ApiClient, resource_ref: ResourceRef, body: dict):
    """
    Creates a new kubernetes resource
    """

    def inner():
        return cast(
            urllib3.response.HTTPResponse,
            kube_client.call_api(
                get_resource_url(resource_ref),
                "POST",
                body=body,
                _return_http_data_only=True,
                _preload_content=False,
            ),
        )

    return await run_sync(inner)


async def update_resource(kube_client: kubernetes.client.ApiClient, resource_ref: ResourceRef, body: dict):
    """
    Replaces an existing kubernetes resource (performing an update).
    """

    def inner():
        return cast(
            urllib3.response.HTTPResponse,
            kube_client.call_api(
                get_resource_url(resource_ref),
                "PUT",
                body=body,
                _return_http_data_only=True,
                _preload_content=False,
            ),
        )

    return await run_sync(inner)


def get_resource_key(resource: dict, key: str) -> str:
    """
    Retrieves a key from a resource.
    """
    if resource["kind"] == "Secret":
        data = resource["data"][key]
        data = base64.b64decode(data).decode("utf-8")
    elif resource["kind"] == "ConfigMap":
        data = resource["data"][key]
    else:
        raise NotImplementedError(resource_fqn(resource))
    return data


class Operator:
    """
    Implements a kubernetes operator capable of syncing minio tenants and
    a handful of custom resource definitions with a remote minio server.
    """

    # health fastapi instance
    health_fastapi: fastapi.FastAPI
    # port used for health endpoint server
    health_port: int
    # a client capable of communcating with kubernetes
    kube_client: kubernetes.client.ApiClient
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
        self.kube_client = cast(kubernetes.client.ApiClient, None)
        self.kube_config = kube_config
        self.logger = logger
        self.ready_event = asyncio.Event()
        self.registry = kopf.OperatorRegistry()

        # register operator hooks
        for hook in iter_hooks(self):
            kopf_decorator_fn = getattr(kopf.on, hook._hook_event)
            kopf_decorator = kopf_decorator_fn(*hook._hook_args, registry=self.registry, **hook._hook_kwargs)
            hook = handle_hook_exception(self.handle_exception, hook)
            hook = log_hook(self.logger, hook)
            kopf_decorator(hook)

        # register health endpoint
        self.health_fastapi.add_api_route("/healthz", self.health, methods=["GET"])

    def handle_exception(self, exception: Exception):
        """
        Operator class hook for exception handling.

        (NOTE: see `handle_hook_exception`)
        """
        pass

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

    def get_resource_type_url(self, resource_type_ref: ResourceTypeRef) -> str:
        """
        Gets a url given a resource type ref
        """
        return get_resource_type_url(resource_type_ref)

    def get_resource_url(self, resource_ref: ResourceRef) -> str:
        """
        Gets a url given a resource ref
        """
        return get_resource_url(resource_ref)

    async def get_resource(self, resource_ref: ResourceRef) -> dict | None:
        """
        Gets a kubernetes resource.  Returns 'None' if the resource does not exist.
        """
        return await get_resource(self.kube_client, resource_ref)

    async def list_resources(self, resource_type_ref: ResourceTypeRef) -> list[dict]:
        """
        Lists kubernetes resources
        """

        return await list_resources(self.kube_client, resource_type_ref)

    async def delete_resource(self, resource_ref: ResourceRef):
        """
        Deletes a kubernetes resource
        """
        return await delete_resource(self.kube_client, resource_ref)

    async def create_resource(self, resource_ref: ResourceRef, body: dict):
        """
        Creates a new kubernetes resource
        """

        return await create_resource(self.kube_client, resource_ref, body)

    async def update_resource(self, resource_ref: ResourceRef, body: dict):
        """
        Replaces an existing kubernetes resource (performing an update).
        """

        return await update_resource(self.kube_client, resource_ref, body)

    async def create_owner_reference(self, resource_ref: ResourceRef) -> dict:
        """
        Creates an owner reference for the given resource ref.

        NOTE: Calls `get_resource` from the kubernetes api
        """
        resource = await self.get_resource(resource_ref)
        if resource is None:
            raise ValueError(f"unable to get resource: {resource_ref.fqn}")
        return {
            "apiVersion": resource["apiVersion"],
            "kind": resource["kind"],
            "name": resource["metadata"]["name"],
            "uid": resource["metadata"]["uid"],
        }

    def get_resource_key(self, resource: dict, key: str) -> str:
        """
        Retrieves a key from a resource.
        """
        return get_resource_key(resource, key)

    @hook("startup")
    async def startup(self, **kwargs):
        """
        Initializes the operator
        """
        self.kube_client = await create_kube_client(self.kube_config)

    @hook("login")
    async def login(self, **kwargs):
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

    async def run(self):
        """
        Runs the operator - and blocks until exit.
        """
        # create healthcheck server
        server = uvicorn.Server(ServerConfig(app=self.health_fastapi, host="0.0.0.0", port=self.health_port))

        await asyncio.gather(
            kopf.operator(clusterwide=True, ready_flag=self.ready_event, registry=self.registry),
            server._serve(),
        )


__all__ = [
    "get_diff",
    "apply_diff_item",
    "filter_immutable_diff_items",
    "create_kube_client",
    "create_resource",
    "delete_resource",
    "list_resources",
    "update_resource",
    "hook",
    "resource_namespace",
    "resource_fqn",
    "run_sync",
    "BaseModel",
    "OperatorError",
    "cluster_namespace",
    "ResourceRef",
    "ResourceKeyRef",
    "Operator",
]
