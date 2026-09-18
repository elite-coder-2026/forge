"""Soft token and compute-time budgets for a session.

Local inference costs no money per token, but a runaway session still
burns real minutes. This warns (never blocks) when a session crosses a
token or model-compute-time limit, so you notice before it eats a quarter
of an hour on something that should have been quick.

A limit of 0 turns that check off. Each limit warns when first crossed and
again at every further multiple (2x, 3x, ...), never on every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BudgetTracker:
    token_limit: int = 0
    minutes_limit: float = 0.0
    _warned: dict[str, int] = field(default_factory=lambda: {"tokens": 0, "minutes": 0})

    def check(self, tokens: int, seconds: float) -> list[str]:
        """New warnings, given the session's totals so far."""
        notices: list[str] = []

        if self.token_limit > 0:
            multiple = tokens // self.token_limit
            if multiple > self._warned["tokens"]:
                self._warned["tokens"] = multiple
                notices.append(
                    f"This session has used {tokens:,} tokens (soft limit "
                    f"{self.token_limit:,}). Long history is resent every step; "
                    f"/clear starts fresh."
                )

        if self.minutes_limit > 0:
            minutes = seconds / 60
            multiple = int(minutes // self.minutes_limit)
            if multiple > self._warned["minutes"]:
                self._warned["minutes"] = multiple
                notices.append(
                    f"This session has used {minutes:.1f} minutes of model compute "
                    f"(soft limit {self.minutes_limit:g}). Consider stopping, or "
                    f"switching to a smaller task or model."
                )
        return notices

    def set_limits(
        self,
        tokens: int | None = None,
        minutes: float | None = None,
        used_tokens: int = 0,
        used_seconds: float = 0.0,
    ) -> None:
        """Change a limit mid-session. Multiples already passed under the
        new limit are treated as warned, so raising or lowering a limit
        doesn't immediately fire a stale alert.
        """
        if tokens is not None:
            self.token_limit = tokens
            self._warned["tokens"] = used_tokens // tokens if tokens > 0 else 0
        if minutes is not None:
            self.minutes_limit = minutes
            self._warned["minutes"] = int((used_seconds / 60) // minutes) if minutes > 0 else 0

    def status(self, tokens: int, seconds: float) -> str:
        def line(label: str, used: str, limit: str) -> str:
            return f"  {label}: {used}" + (f" of {limit}" if limit else " (no limit)")

        return "\n".join(
            [
                "Session budget (warnings only; nothing is blocked):",
                line(
                    "tokens",
                    f"{tokens:,}",
                    f"{self.token_limit:,}" if self.token_limit > 0 else "",
                ),
                line(
                    "model compute",
                    f"{seconds / 60:.1f} min",
                    f"{self.minutes_limit:g} min" if self.minutes_limit > 0 else "",
                ),
            ]
        )
