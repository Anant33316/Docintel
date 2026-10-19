"""Shared code for the DocIntel Lambdas (deployed as a Lambda layer).

Modules are deliberately small and mostly pure so they can be unit tested
without AWS. Every AWS handle is obtained through ``docintel_common.aws`` so
tests can swap in in-memory fakes.
"""
