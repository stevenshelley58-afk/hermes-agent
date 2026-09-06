"""Legacy import alias for gateway.ad_template_generator_process.

The persisted process ID remains exact-clone; active implementation code uses
the product's canonical Ad Template Generator name.
"""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("gateway.ad_template_generator_process")
