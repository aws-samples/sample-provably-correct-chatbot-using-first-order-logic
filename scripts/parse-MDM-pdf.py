# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Parse the contents of the table
"""
import argparse, os, sys
from typing import List


import pdfplumber

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)


if __name__ == "__main__":
    with pdfplumber.open("2023-e-m-descriptors-guidelines.pdf") as pdf:
        first_page = pdf.pages[0]
        print(first_page.chars[0])
        tables = first_page.find_tables()
        print(f"Found {len(tables):,} tables")