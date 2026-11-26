import nemogate
from nemogate.gate import Guard, GuardLoopError


def test_the_package_root_exports_the_guard_and_its_loop_error_and_a_version():
    # Breaks if `from nemogate import Guard` stops working or `__version__` is not a string.
    assert nemogate.Guard is Guard
    assert nemogate.GuardLoopError is GuardLoopError
    assert nemogate.__version__ == "0.1.0"
