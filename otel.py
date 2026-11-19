from os import environ
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Optional

from globals import DO_USE_OTEL

from dotenv import load_dotenv
from langfuse import Langfuse, get_client, LangfuseSpan, propagate_attributes
from opentelemetry.context import attach, get_current
from opentelemetry.propagate import extract, inject
from opentelemetry import trace
from strands import Agent


load_dotenv()

if DO_USE_OTEL:
    langfuse = Langfuse(
        public_key=environ['LANGFUSE_PUBLIC_KEY'],
        secret_key=environ['LANGFUSE_SECRET_KEY'],
        host="https://us.cloud.langfuse.com")
else:
    langfuse = None


# Exports

langfuse_client = get_client() if langfuse else None
# print(f"langfuse_client: {langfuse_client}")
Span = LangfuseSpan
otel_flush = langfuse.flush if langfuse else None
get_current_trace_id = langfuse.get_current_trace_id if langfuse else None

_SESSION_ID: Optional[str] = None

def otel_session_id():
    """ Just has to be < 200 chars """
    global _SESSION_ID
    if _SESSION_ID is None:
        mountain_time_zone = ZoneInfo('America/Denver')
        current_time_mt = datetime.now(mountain_time_zone)
        curr_dt = current_time_mt.strftime("%b %d, %y %H:%M:%S %Z%z")
        _SESSION_ID = curr_dt.replace(" ", "-")
    return _SESSION_ID


class NullSpan:
    def __init__(self, name: str):
        self.result_message: Optional[str] = None

    def result(self, message: str):
        self.result_message = message

    def update(self, *args, **kwargs):
        pass


@contextmanager
def start_span(name: str, **attributes):
    print(f"start span: {name}")
    if DO_USE_OTEL:
        with langfuse_client.start_as_current_span(name=name) as span:
            langfuse_client.flush()
            span.update(input=attributes)
            try:
                yield span
            finally:
                langfuse_client.flush()
                print(f"end span: {name}")
    else:
        start_time = time.time()
        try:
            span = NullSpan(name=name)
            yield span
        finally:
            try:
                result_message = span.result_message
            except Exception as ex:
                result_message = f"Failed to get result message: {ex}"
                result_message = None
            duration = time.time() - start_time
            result_str = f" -> {result_message}" if result_message else ""
            print(f"end span: {name}{result_str} duration {duration/60:.2f} mins")


def worker_function(i: int, trace_context: dict):
    print(f"Worker {i} starting with trace context: {trace_context}")
    attach(extract(trace_context))
    # print(f"after extract, span: {langfuse_client._get_current_otel_span()}")
    with start_span(name=f"worker-span-{i}") as span:
        # print(f"worker: start {langfuse_client._get_current_otel_span()}")
        # print(f"Worker {i} trace ID: {span._langfuse_client.get_current_trace_id()}")
        span.update(input=i)
        span.update(output=i * i)
        with start_span(name=f"worker-inner-span-{i}") as inner_span:
            # print(f"worker: inner {langfuse_client._get_current_otel_span()}")
            inner_span.update(input=i+2)
            inner_span.update(output=i+3)
            agent = Agent(callback_handler=None)
            response = agent("What is 42 + 1?")
            print(response)

            agent = Agent(callback_handler=None)
            response = agent("What is 1 + 1?")
            print(response)
    return i * i


if __name__ == "__main__":
    import multiprocessing as mp
    ctx = mp.get_context('spawn')

    with start_span("root-span", x=42) as root:
        print(f"Root trace ID: {get_current_trace_id()}")
        with start_span("child-span", y=43) as child:
            print("Hello world! Data should be in Langfuse now.")
            print(f"Child trace ID: {get_current_trace_id()}")
            print(f"child ID: {child.id}")
            # print(f"Child trace ID: {child.get_current_trace_id()}")
            # print(dir(child))
            # print(child._langfuse_client)
            # print(dir(child._langfuse_client))
            # print(child._langfuse_client._get_current_otel_span())
            # print(child._langfuse_client.get_current_trace_id())
            # child.update(input=42)
            # child.update(output=43)
            # print(f"Parent trace ID: {child._langfuse_client.get_current_trace_id()}")
            # context = trace.get_current_span().get_span_context() # Get the current context
            # print(f"Current context: {context}")
            trace_context = {}
            inject(trace_context, get_current())
            # print(f"get_current(): {get_current()}")
            # print(f"Trace context for workers: {trace_context}")

            p = ctx.Process(target=worker_function, args=(0, trace_context))
            p.start()
            p.join()

            # with ctx.Pool(processes=4) as pool:
            #     results = pool.starmap(worker_function, [[x, trace_context] for x in range(4)])
            # print(f"Worker results: {results}")
