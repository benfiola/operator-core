import asyncio
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

        setattr(f, "_hook_fn", True)
        setattr(f, "_hook_event", event)
        setattr(f, "_hook_args", args)
        setattr(f, "_hook_kwargs", kwargs)

        return cast(HookFn[WrappedFn], f)

    return inner


def iter_hooks(obj: object) -> Iterable[HookFn]:
    """
    Given an object, iterates over instance members and yields functions decorated with the `hook` decorator.
    """
    for attr in dir(obj):
        val = getattr(obj, attr)
        if not callable(val):
            continue
        if not hasattr(val, "__dict__"):
            continue
        if "_hook_fn" not in val.__dict__:
            continue
        val = cast(HookFn, val)
        yield val


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


class ResourceRefSpec(BaseModel):
    """
    Represents a reference to another kubernetes resource as defined in a resource spec.

    The 'namespace' field is optional - if 'None' assumed to be the current namespace
    of the containing object.
    """

    name: str
    namespace: str | None = None

    def resource_ref(self, default_namespace: str) -> "ResourceRef":
        """
        Convenience method to create a 'ResourceRef' object.

        Will set the namespace to 'default_namespace' if namespace is None.
        """
        return ResourceRef(
            name=self.name, namespace=self.namespace or default_namespace
        )


class ResourceKeyRefSpec(ResourceRefSpec):
    """
    Represents a reference to a key of a kubernetes resource as defined in a resource spec.
    """

    key: str


class ResourceRef(BaseModel):
    """
    Represents a reference to a kubernetes resource.  Differs from `ResourceRefSpec` in that the namespace
    field *must* be set.
    """

    name: str
    namespace: str

    @property
    def fqn(self) -> str:
        return f"{self.namespace}/{self.name}"


class ResourceKeyRef(ResourceRef):
    """
    Represents a reference to a key of a kubernetes resource as defined in a resource.
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
            kopf_decorator = kopf_decorator_fn(
                *hook._hook_args, registry=self.registry, **hook._hook_kwargs
            )
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

    @hook("startup")
    async def startup(self, **kwargs):
        """
        Initializes the operator
        """
        config = kubernetes.client.Configuration()
        if self.kube_config:
            self.logger.debug(f"kube client using kubeconfig: {self.kube_config}")
            kubernetes.config.load_kube_config(
                config_file=f"{self.kube_config}", client_configuration=config
            )
        else:
            self.logger.debug(f"kube client using in-cluster")
            kubernetes.config.load_incluster_config(client_configuration=config)
        self.kube_client = kubernetes.client.ApiClient(config)

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
    "apply_diff_item",
    "filter_immutable_diff_items",
    "get_diff",
    "hook",
    "resource_namespace",
    "resource_fqn",
    "run_sync",
    "BaseModel",
    "Operator",
    "OperatorError",
    "ResourceRef",
    "ResourceKeyRef",
    "ResourceRefSpec",
    "ResourceKeyRefSpec",
]
