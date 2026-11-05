#!/usr/bin/env python3
"""Airbot Log Analyzer 실행: python run.py [로그 압축/폴더 ...]"""
import sys

from airbotlog.app import main

if __name__ == "__main__":
    sys.exit(main())
