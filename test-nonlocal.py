# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


def foo():
    num: int = -1

    def bar():
        nonlocal num
        print(f"bar: {num}")
        num := num + 1
        print(f"bar: {num}")

    bar()
    bar()
    bar()

foo()
