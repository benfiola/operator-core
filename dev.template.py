import asyncio
import logging
import pathlib

from operator_core import BaseModel, NamespacedResource, Operator

logger = logging.getLogger(__name__)


class TestSpec(BaseModel):
    mutable: str
    immutable: str


class Namespaced(NamespacedResource[TestSpec]):
    __oc_resource__ = {
        "api_version": "bfiola.dev/v1",
        "kind": "Namespaced",
        "plural": "namespaceds",
    }
    __oc_immutable_fields__ = {("immutable",)}


class Global(NamespacedResource[TestSpec]):
    __oc_resource__ = {
        "api_version": "bfiola.dev/v1",
        "kind": "Global",
        "plural": "globals",
    }
    __oc_immutable_fields__ = {("immutable",)}


class TestOperator(Operator):
    def __init__(self, **kwargs):
        kwargs["logger"] = logger
        super().__init__(**kwargs)
        self.watch_resource(
            Namespaced, on_create=self.sync, on_update=self.sync, on_delete=self.delete
        )
        self.watch_resource(
            Global, on_create=self.sync, on_update=self.sync, on_delete=self.delete
        )

    async def sync(self, resource: Namespaced | Global, **kwargs):
        pass

    async def delete(self, resource: Namespaced | Global, **kwargs):
        pass


async def main():
    operator = TestOperator(
        kube_config=pathlib.Path("/root/.kube/config"), logger=logger
    )
    await operator.run()


if __name__ == "__main__":
    logger = logging.getLogger("dev")
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
