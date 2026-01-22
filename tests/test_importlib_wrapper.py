"""
Unit tests for importlib_wrapper module.
"""

import os
import tempfile
import unittest
from unittest import mock

import importlib_wrapper


class TestLoadSource(unittest.TestCase):
    """Test case for load_source function."""

    def test_load_source_basic_module(self):
        """Test loading a simple Python module."""
        # Create a temporary Python file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('test_variable = "hello world"\n')
            f.write("def test_function():\n")
            f.write("    return 42\n")
            temp_file = f.name

        try:
            # Load the module
            module = importlib_wrapper.load_source("test_module", temp_file)

            # Verify the module was loaded correctly
            self.assertEqual(module.__name__, "test_module")
            self.assertEqual(module.test_variable, "hello world")
            self.assertEqual(module.test_function(), 42)
        finally:
            # Cleanup
            os.unlink(temp_file)

    def test_load_source_with_imports(self):
        """Test loading a module that imports standard library modules."""
        # Create a temporary Python file with imports
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("import os\n")
            f.write("import sys\n")
            f.write("def get_platform():\n")
            f.write("    return sys.platform\n")
            temp_file = f.name

        try:
            # Load the module
            module = importlib_wrapper.load_source("test_module_imports", temp_file)

            # Verify the module has access to imports
            self.assertTrue(hasattr(module, "os"))
            self.assertTrue(hasattr(module, "sys"))
            self.assertIsNotNone(module.get_platform())
        finally:
            # Cleanup
            os.unlink(temp_file)

    def test_load_source_nonexistent_file(self):
        """Test that loading a nonexistent file raises an error."""
        with self.assertRaises(FileNotFoundError):
            importlib_wrapper.load_source("nonexistent", "/path/to/nonexistent/file.py")

    def test_load_source_syntax_error(self):
        """Test that loading a file with syntax errors raises SyntaxError."""
        # Create a temporary Python file with syntax error
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def invalid syntax here\n")
            temp_file = f.name

        try:
            with self.assertRaises(SyntaxError):
                importlib_wrapper.load_source("invalid_module", temp_file)
        finally:
            # Cleanup
            os.unlink(temp_file)

    def test_load_source_returns_module_type(self):
        """Test that load_source returns a module type."""
        from types import ModuleType

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("# Empty module\n")
            temp_file = f.name

        try:
            module = importlib_wrapper.load_source("empty_module", temp_file)
            self.assertIsInstance(module, ModuleType)
        finally:
            os.unlink(temp_file)


if __name__ == "__main__":
    unittest.main()
