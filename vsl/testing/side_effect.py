"""Observable consequential operations for governance tests.

A governed action must never run its side effect unless governance
permits it. These helpers make that checkable without mocking away the
behaviour under test: the effect is a real callable that records every
call, optionally raises, and can snapshot arbitrary state at the moment
it runs (for example the ledger, to prove governance evidence existed
*before* the effect).
"""

from __future__ import annotations

from typing import Any, Callable


class SideEffect:
    """A synchronous consequential operation that records its calls.

    ``result``  value returned when the effect runs.
    ``raises``  exception instance or class raised when the effect runs.
                The call is recorded before raising, so a failing effect
                still counts as executed.
    ``observe`` zero-argument callable evaluated at call time, before the
                effect does anything; its value is appended to
                ``observations``.
    """

    def __init__(
        self,
        result: Any = None,
        *,
        raises: BaseException | type[BaseException] | None = None,
        observe: Callable[[], Any] | None = None,
        name: str = "side-effect",
    ) -> None:
        self.name = name
        self.result = result
        self.raises = raises
        self._observe = observe
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.observations: list[Any] = []

    # -- recording ---------------------------------------------------

    def _record(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if self._observe is not None:
            self.observations.append(self._observe())

        self.calls.append((args, kwargs))

    def _finish(self) -> Any:
        if self.raises is not None:
            raise self.raises

        return self.result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._record(args, kwargs)
        return self._finish()

    # -- inspection --------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def executed(self) -> bool:
        return bool(self.calls)

    def assert_not_executed(self, context: str = "") -> None:
        if self.calls:
            raise AssertionError(
                f"{self.name} executed {len(self.calls)} time(s) but "
                "governance did not permit it"
                + (f" ({context})" if context else "")
            )

    def assert_executed(self, times: int = 1) -> None:
        if len(self.calls) != times:
            raise AssertionError(
                f"{self.name} executed {len(self.calls)} time(s); "
                f"expected {times}"
            )


class AsyncSideEffect(SideEffect):
    """Same recording behaviour for an ``async def`` operation."""

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self._record(args, kwargs)
        return self._finish()
