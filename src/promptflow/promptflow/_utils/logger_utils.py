# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

# This file is for open source,
# so it should not contain any dependency on azure or azureml-related packages.


import logging
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import List, Optional

from promptflow._utils.credential_scrubber import CredentialScrubber
from promptflow.contracts.run_mode import RunMode

# The maximum length of logger name is 18 ("promptflow-runtime").
# The maximum digit length of process id is 5. Fix the field width to 7.
# So fix the length of these fields in the formatter.
# May need to change if logger name/process id length changes.
LOG_FORMAT = "%(asctime)s %(process)7d %(name)-18s %(levelname)-8s %(message)s"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"


class CredentialScrubberFormatter(logging.Formatter):
    """Formatter that scrubs credentials in logs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._default_scrubber = CredentialScrubber()
        self._context_var = ContextVar("credential_scrubber", default=None)

    @property
    def credential_scrubber(self):
        credential_scrubber = self._context_var.get()
        if credential_scrubber:
            return credential_scrubber
        return self._default_scrubber

    def set_credential_list(self, credential_list: List[str]):
        """Set credential list, which will be scrubbed in logs."""
        credential_scrubber = CredentialScrubber()
        for c in credential_list:
            credential_scrubber.add_str(c)
        self._context_var.set(credential_scrubber)

    def clear(self):
        """Clear context variable."""
        self._context_var.set(None)

    def format(self, record):
        """Override logging.Formatter's format method and remove credentials from log."""
        s: str = super().format(record)

        s = self._handle_traceback(s, record)
        s = self._handle_customer_content(s, record)
        return self.credential_scrubber.scrub(s)

    def _handle_customer_content(self, s: str, record: logging.LogRecord) -> str:
        """Interface method for handling customer content in log message.

        Derived class can override this method to handle customer content in log.
        """
        return s

    def _handle_traceback(self, s: str, record: logging.LogRecord) -> str:
        """Interface method for handling traceback in log message.

        Derived class can override this method to handle traceback in log.
        """
        return s


class FileHandler:
    """Write compliant log to a file."""

    def __init__(self, file_path: str, formatter: Optional[logging.Formatter] = None):
        self._stream_handler = self._get_stream_handler(file_path)
        if formatter is None:
            # Default formatter to scrub credentials in log message, exception and stack trace.
            self._formatter = CredentialScrubberFormatter(fmt=LOG_FORMAT, datefmt=DATETIME_FORMAT)
        else:
            self._formatter = formatter
        self._stream_handler.setFormatter(self._formatter)

    def set_credential_list(self, credential_list: List[str]):
        """Set credential list, which will be scrubbed in logs."""
        self._formatter.set_credential_list(credential_list)

    def emit(self, record: logging.LogRecord):
        """Write logs."""
        self._stream_handler.emit(record)

    def close(self):
        """Close stream handler."""
        self._stream_handler.close()
        self._formatter.clear()

    def _get_stream_handler(self, file_path) -> logging.StreamHandler:
        """This method can be overridden by derived class to save log file in cloud."""
        return logging.FileHandler(file_path, encoding="UTF-8")


class FileHandlerConcurrentWrapper(logging.Handler):
    """Wrap context-local FileHandler instance for thread safety.

    A logger instance can write different log to different files in different contexts.
    """

    def __init__(self):
        super().__init__()
        self._context_var = ContextVar("handler", default=None)

    @property
    def handler(self) -> FileHandler:
        return self._context_var.get()

    @handler.setter
    def handler(self, handler: FileHandler):
        self._context_var.set(handler)

    def emit(self, record: logging.LogRecord):
        """Override logging.Handler's emit method.

        Get inner file handler in current context and write log.
        """
        stream_handler: FileHandler = self._context_var.get()
        if stream_handler is None:
            return
        stream_handler.emit(record)

    def clear(self):
        """Close file handler and clear context variable."""
        handler: FileHandler = self._context_var.get()
        if handler:
            try:
                handler.close()
            except:  # NOQA: E722
                # Do nothing if handler close failed.
                pass
        self._context_var.set(None)


def get_logger(name: str) -> logging.Logger:
    logger = logging.Logger(name)
    logger.addHandler(FileHandlerConcurrentWrapper())
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(CredentialScrubberFormatter(fmt=LOG_FORMAT, datefmt=DATETIME_FORMAT))
    logger.addHandler(stdout_handler)
    return logger


# Logs by flow_logger will only be shown in flow mode.
# These logs should contain all detailed logs from executor and runtime.
flow_logger = get_logger("execution.flow")

# Logs by bulk_logger will only be shown in bulktest and eval modes.
# These logs should contain overall progress logs and error logs.
bulk_logger = get_logger("execution.bulk")

# Logs by logger will be shown in all the modes above,
# such as error logs.
logger = get_logger("execution")


logger_contexts = []


@dataclass
class LogContext:
    """A context manager to setup logger context for input_logger, logger, flow_logger and bulk_logger."""

    file_path: str  # Log file path.
    run_mode: Optional[RunMode] = RunMode.Test
    credential_list: Optional[List[str]] = None  # These credentials will be scrubbed in logs.
    input_logger: logging.Logger = None  # If set, then context will also be set for input_logger.

    def get_initializer(self):
        return partial(
            LogContext, file_path=self.file_path, run_mode=self.run_mode, credential_list=self.credential_list
        )

    @staticmethod
    def get_current() -> Optional["LogContext"]:
        global logger_contexts
        if logger_contexts:
            return logger_contexts[-1]
        return None

    @staticmethod
    def set_current(context: "LogContext"):
        global logger_contexts
        if isinstance(context, LogContext):
            logger_contexts.append(context)

    @staticmethod
    def clear_current():
        global logger_contexts
        if logger_contexts:
            logger_contexts.pop()

    def __enter__(self):
        self._set_log_path()
        self._set_credential_list()
        LogContext.set_current(self)

    def __exit__(self, *args):
        """Clear context-local variables."""
        all_logger_list = [logger, flow_logger, bulk_logger]
        if self.input_logger:
            all_logger_list.append(self.input_logger)
        for logger_ in all_logger_list:
            for handler in logger_.handlers:
                if isinstance(handler, FileHandlerConcurrentWrapper):
                    handler.clear()
                elif isinstance(handler.formatter, CredentialScrubberFormatter):
                    handler.formatter.clear()
        LogContext.clear_current()

    def _set_log_path(self):
        if not self.file_path:
            return

        logger_list = self._get_loggers_to_set_path()
        for logger_ in logger_list:
            for log_handler in logger_.handlers:
                if isinstance(log_handler, FileHandlerConcurrentWrapper):
                    handler = FileHandler(self.file_path)
                    log_handler.handler = handler

    def _set_credential_list(self):
        # Set credential list to all loggers.
        all_logger_list = self._get_execute_loggers_list()
        if self.input_logger:
            all_logger_list.append(self.input_logger)
        credential_list = self.credential_list or []
        for logger_ in all_logger_list:
            for handler in logger_.handlers:
                if isinstance(handler, FileHandlerConcurrentWrapper) and handler.handler:
                    handler.handler.set_credential_list(credential_list)
                elif isinstance(handler.formatter, CredentialScrubberFormatter):
                    handler.formatter.set_credential_list(credential_list)

    def _get_loggers_to_set_path(self) -> List[logging.Logger]:
        logger_list = [logger]
        if self.input_logger:
            logger_list.append(self.input_logger)

        # For Batch run mode, set log path for bulk_logger,
        # otherwise for flow_logger.
        if self.run_mode == RunMode.Batch:
            logger_list.append(bulk_logger)
        else:
            logger_list.append(flow_logger)
        return logger_list

    @classmethod
    def _get_execute_loggers_list(cls) -> List[logging.Logger]:
        # return all loggers for executor
        return [logger, flow_logger, bulk_logger]


def update_log_path(log_path: str, input_logger: logging.Logger = None):
    logger_list = [logger, bulk_logger, flow_logger]
    if input_logger:
        logger_list.append(input_logger)
    for logger_ in logger_list:
        for wrapper in logger_.handlers:
            if isinstance(wrapper, FileHandlerConcurrentWrapper):
                handler: FileHandler = wrapper.handler
                if handler:
                    wrapper.handler = type(handler)(log_path, handler._formatter)


def scrub_credentials(s: str):
    """Scrub credentials in string s.

    For example, for input string: "print accountkey=accountKey", the output will be:
    "print accountkey=**data_scrubbed**"
    """
    for h in logger.handlers:
        if isinstance(h, FileHandlerConcurrentWrapper):
            if h.handler and h.handler._formatter:
                credential_scrubber = h.handler._formatter.credential_scrubber
                if credential_scrubber:
                    return credential_scrubber.scrub(s)
    return CredentialScrubber().scrub(s)
