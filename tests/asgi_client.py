"""Small synchronous facade over the installed HTTPX ASGI transport, with lifespan.

Avoid Starlette's deprecated httpx1 TestClient fallback, without warning filters,
vendoring or a second HTTP client dependency. HTTP still traverses the full ASGI app.
"""
from contextlib import ExitStack
from functools import partial

from anyio.from_thread import start_blocking_portal
import httpx


class ASGITestClient:
    __test__ = False

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self.stack = ExitStack()
        try:
            self.portal = self.stack.enter_context(start_blocking_portal())
            self.stack.enter_context(self.portal.wrap_async_context_manager(self.app.router.lifespan_context(self.app)))
            self.client = self.stack.enter_context(self.portal.wrap_async_context_manager(httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url='http://testserver', follow_redirects=True)))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def request(self, method, path, **kwargs):
        return self.portal.call(partial(self.client.request, method, path, **kwargs))

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)
