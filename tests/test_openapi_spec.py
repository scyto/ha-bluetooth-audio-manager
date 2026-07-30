"""Guard the hand-written OpenAPI spec against drift.

`web/api.py` handlers are all `(request: web.Request) -> web.Response`, so
nothing can generate `static/openapi.yaml` from the code — it is maintained by
hand. This test is what keeps it honest: it enumerates the routes the app
actually registers and asserts a 1:1 match with the spec.

It catches a *missing or extra endpoint*. It cannot tell whether a documented
request or response body still matches reality — check that by hand when you
change a handler.

See docs/roadmap.md for the deferred plan to generate the spec instead.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on incomplete envs
    yaml = None

try:
    from bt_audio_manager.web.api import OPENAPI_PATH, create_api_routes
    IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - libpulse missing on some dev boxes
    IMPORT_ERROR = e

_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


@unittest.skipIf(IMPORT_ERROR is not None, f"web.api not importable: {IMPORT_ERROR}")
@unittest.skipIf(yaml is None, "PyYAML not installed")
class OpenApiSpecTest(unittest.TestCase):
    """The spec and the router must describe the same API."""

    @classmethod
    def setUpClass(cls):
        cls.spec = yaml.safe_load(Path(OPENAPI_PATH).read_text())
        # create_api_routes only stores the manager for use inside handlers,
        # so a bare mock is enough to build the route table.
        cls.routes = create_api_routes(MagicMock())

    def _registered(self) -> set[tuple[str, str]]:
        """(METHOD, path) for every route the app registers."""
        return {
            (r.method.upper(), r.path)
            for r in self.routes
            if isinstance(r, __import__("aiohttp").web.RouteDef)
        }

    def _documented(self) -> set[tuple[str, str]]:
        """(METHOD, path) for every operation in the spec."""
        return {
            (method.upper(), path)
            for path, item in self.spec["paths"].items()
            for method in item
            if method.lower() in _METHODS
        }

    def test_every_route_is_documented(self):
        missing = self._registered() - self._documented()
        self.assertFalse(
            missing,
            "Endpoints exist but are missing from static/openapi.yaml: "
            + ", ".join(f"{m} {p}" for m, p in sorted(missing)),
        )

    def test_no_documented_route_is_gone(self):
        stale = self._documented() - self._registered()
        self.assertFalse(
            stale,
            "static/openapi.yaml documents endpoints that no longer exist: "
            + ", ".join(f"{m} {p}" for m, p in sorted(stale)),
        )

    def test_spec_is_structurally_sound(self):
        """Cheap sanity checks — a malformed spec renders as a blank page."""
        self.assertTrue(self.spec.get("openapi", "").startswith("3."))
        self.assertIn("title", self.spec.get("info", {}))
        self.assertTrue(self.spec.get("paths"))

    def test_every_operation_documents_a_success_response(self):
        """An operation with no 2xx tells the reader nothing useful."""
        for path, item in self.spec["paths"].items():
            for method, op in item.items():
                if method.lower() not in _METHODS:
                    continue
                with self.subTest(endpoint=f"{method.upper()} {path}"):
                    codes = [str(c) for c in op.get("responses", {})]
                    self.assertTrue(
                        any(c.startswith("2") or c == "101" for c in codes),
                        f"no success response documented (got {codes})",
                    )

    def test_every_operation_has_a_summary_and_tag(self):
        """Swagger UI groups by tag and lists by summary; both must be present."""
        known_tags = {t["name"] for t in self.spec.get("tags", [])}
        for path, item in self.spec["paths"].items():
            for method, op in item.items():
                if method.lower() not in _METHODS:
                    continue
                with self.subTest(endpoint=f"{method.upper()} {path}"):
                    self.assertTrue(op.get("summary"), "missing summary")
                    tags = op.get("tags", [])
                    self.assertTrue(tags, "missing tag")
                    for tag in tags:
                        self.assertIn(tag, known_tags, "tag not declared at top level")

    def test_all_local_refs_resolve(self):
        """A typo'd $ref renders as an empty schema rather than an error."""
        targets = []

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and isinstance(value, str):
                        targets.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.spec)
        self.assertTrue(targets, "expected the spec to use $ref")
        for ref in targets:
            with self.subTest(ref=ref):
                self.assertTrue(ref.startswith("#/"), "only local refs are supported")
                node = self.spec
                for part in ref[2:].split("/"):
                    self.assertIn(part, node, f"unresolved $ref: {ref}")
                    node = node[part]


if __name__ == "__main__":
    unittest.main()
