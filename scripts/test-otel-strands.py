# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test if attributes get propagated up properly.
"""
import os, sys

from strands import Agent

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

from otel import start_span, get_current_trace_id


if __name__ == "__main__":
    with start_span("root-span") as root:
        print(f"Trace ID: {get_current_trace_id()}")
        with start_span("child-span") as child:
            print(dir(child))
            print(child.__dict__ )
            print(child._otel_span)
            agent = Agent(callback_handler=None)
            response = agent("What is 42 + 1?")
            print(response)

            agent = Agent(callback_handler=None)
            response = agent("What is 1 + 1?")
            print(response)