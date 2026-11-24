# -*- coding: utf-8 -*-
"""verify 单一入口：跑 tests/ 下全部 unittest，失败以非零退出码上报。"""
import os
import sys
import unittest

if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, root)
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
