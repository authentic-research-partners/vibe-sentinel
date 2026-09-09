"""The labelled corpora: commands, files, package names, drift reports.

One module per judgement, each exporting ``CASES`` and whatever scene the
cases need planted around them. Nothing in here runs anything — a corpus
is data, and keeping it that way is what lets the same cases be measured
against a different model, a different prompt, or a different combining
rule without touching them.
"""
