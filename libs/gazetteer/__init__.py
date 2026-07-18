"""Firehose gazetteer matcher (PLAN §6.3 step 2): cheap entity/geo tally for velocity."""

from libs.gazetteer.matcher import GazEntry, Gazetteer, load_gazetteer

__all__ = ["GazEntry", "Gazetteer", "load_gazetteer"]
