from enum import StrEnum


class AlertState(StrEnum):
    FIRING = "FIRING"
    RESOLVED = "RESOLVED"


class ClassificationStatus(StrEnum):
    CLASSIFIED = "CLASSIFIED"
    UNCLASSIFIED = "UNCLASSIFIED"
