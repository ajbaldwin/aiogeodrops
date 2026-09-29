"""Exceptions raised by aiogeodrops."""

from __future__ import annotations


class GeoDropsError(Exception):
    """Base class for every aiogeodrops error."""


class GeoDropsCredentialsError(GeoDropsError):
    """The service-account key is malformed or unusable."""


class GeoDropsAuthError(GeoDropsError):
    """Google rejected the service-account key.

    The key was deleted or revoked, or its service account disabled. Only a
    new key fixes this.
    """


class GeoDropsConnectionError(GeoDropsError):
    """Google could not be reached, or failed in a way that clears on its own.

    Covers network errors, timeouts, 5xx responses, and rate or quota limits.
    Retrying later is the right response.
    """


class GeoDropsQueryError(GeoDropsError):
    """BigQuery rejected the query."""


class GeoDropsAccessDeniedError(GeoDropsQueryError):
    """BigQuery refused the query with a 403.

    The service account lacks a role (BigQuery Job User) or the BigQuery API
    is not enabled on the project. The key is fine; the fix is in Google
    Cloud, after which the next query succeeds.
    """
