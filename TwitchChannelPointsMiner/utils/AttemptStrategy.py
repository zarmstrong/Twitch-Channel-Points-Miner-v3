import logging
import time
from typing import Callable, Generic, TypeVar

TResult = TypeVar("TResult")

logger = logging.getLogger(__name__)


class ExceptionContext:
    """Utility for printing a stack trace outside the context of an exception handler."""

    def __init__(self, exception: Exception, stack_trace: str | None):
        self.exception = exception
        self.stack_trace = stack_trace

    def __repr__(self):
        if self.stack_trace is not None:
            return f"{self.stack_trace}\n{self.exception}"
        else:
            return f"{self.exception}"

    def __eq__(self, other):
        return (
            isinstance(other, ExceptionContext)
            and self.exception == other.exception
            and self.stack_trace == other.stack_trace
        )


class SuccessResult(Generic[TResult]):
    """Returned when the result of `make_attempts` was successful."""

    def __init__(self, errors: list[ExceptionContext], result: TResult):
        self.errors = errors
        """Any errors that occurred."""
        self.result = result
        """The result."""

    @property
    def attempts(self):
        """The number of attempts made."""
        return len(self.errors) + 1

    def __repr__(self):
        return f"SuccessResult({self.__dict__})"

    def __eq__(self, other):
        if isinstance(other, SuccessResult) and len(self.errors) == len(other.errors):
            for index in range(len(self.errors)):
                if self.errors[index] != other.errors[index]:
                    return False
            return self.result == other.result
        return False


class ErrorResult:
    """Returned when the result of `make_attempts` was 1 or more errors."""

    def __init__(self, errors: list[ExceptionContext]):
        self.errors = errors
        """The errors in the order they occurred."""

    @property
    def attempts(self):
        """The number of attempts made."""
        return len(self.errors)

    def __repr__(self):
        return f"ErrorResult({self.__dict__})"

    def __eq__(self, other):
        if isinstance(other, ErrorResult) and len(self.errors) == len(other.errors):
            for index in range(len(self.errors)):
                if self.errors[index] != other.errors[index]:
                    return False
            return True
        return False


class AttemptStrategy:
    """Handles making an attempt at something multiple times by catching Exceptions and validating the Result."""

    def __init__(
        self,
        attempts: int = 3,
        attempt_interval_seconds: int | float = 1,
        backoff_multiplier: int | float = 1,
        max_interval_seconds: int | float | None = None,
    ):
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if (
            isinstance(attempt_interval_seconds, bool)
            or not isinstance(attempt_interval_seconds, (int, float))
            or attempt_interval_seconds < 0
        ):
            raise ValueError("attempt_interval_seconds must be a non-negative number")
        if (
            isinstance(backoff_multiplier, bool)
            or not isinstance(backoff_multiplier, (int, float))
            or backoff_multiplier < 1
        ):
            raise ValueError(
                "backoff_multiplier must be a number greater than or equal to 1"
            )
        if max_interval_seconds is not None and (
            isinstance(max_interval_seconds, bool)
            or not isinstance(max_interval_seconds, (int, float))
            or max_interval_seconds < 0
        ):
            raise ValueError(
                "max_interval_seconds must be a non-negative number or None"
            )
        self.attempts = attempts
        """The number of attempts that should be made."""
        self.attempt_interval_seconds = attempt_interval_seconds
        """Base number of seconds to wait between attempts before backoff."""
        self.backoff_multiplier = backoff_multiplier
        """Multiplier applied to the base wait after each failed attempt."""
        self.max_interval_seconds = max_interval_seconds
        """Optional upper bound for the wait between attempts."""

    def _interval_after(self, failed_attempts: int) -> float:
        interval = self.attempt_interval_seconds * (
            self.backoff_multiplier ** (failed_attempts - 1)
        )
        if self.max_interval_seconds is not None:
            interval = min(interval, self.max_interval_seconds)
        return interval

    def make_attempts(
        self,
        attempt: Callable[[], TResult],
        validate: Callable[[TResult], None],
        retryable: Callable[[Exception], bool],
        exception_context: Callable[[Exception], str | None],
    ) -> SuccessResult[TResult] | ErrorResult:
        """
        Calls `attempt` up to `self.attempts` times until either a successful attempt is made or the maximum number of
        attempts have been made.
        :param attempt: The function to attempt.
        :param validate: Function to check if the result is valid. Should throw an Exception if not.
        :param retryable: Function that returns True if a given Error can be retried.
        :param exception_context: Function that returns a context string (or None) for a given Exception.
        :return:
        """
        attempts = 0
        errors: list[ExceptionContext] = []
        while attempts < self.attempts:
            attempts += 1
            try:
                result = attempt()
                validate(result)
                return SuccessResult(errors, result)
            except Exception as e:
                errors.append(ExceptionContext(e, exception_context(e)))
                if not retryable(e):
                    logger.debug(f"Error cannot be retried: {e}")
                    break
            if attempts >= self.attempts:
                # Break early to avoid sleeping
                break
            else:
                time.sleep(self._interval_after(attempts))
        return ErrorResult(errors)
