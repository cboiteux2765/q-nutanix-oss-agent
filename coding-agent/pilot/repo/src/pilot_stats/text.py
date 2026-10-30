"""Small text helpers."""
import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def word_count(text):
    return len(text.split())
