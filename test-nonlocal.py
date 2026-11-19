
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
