"""The company universe.

A second index alongside the repositories, built on the same machinery: a
universe, a classification, a daily series, and the same four boards plus three
that only make sense for companies (funding, valuation, acquisitions).

Nothing here re-implements scoring. `airadar.scoring.metrics` takes a daily
series and returns velocity, acceleration, fresh power and a breakout flag; it
does not care whether the series counts GitHub stars or monthly visits.
"""
