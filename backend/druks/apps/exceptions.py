class AppConfigError(Exception):
    """An app's ``.druks`` config could not be parsed or validated.

    Raised at intake (and by push-time validation) so bad config fails
    loudly where the work starts instead of half-applying."""


class SettingsDeclarationError(Exception):
    """A ``Settings`` inner class declares a field the settings plane can't render or
    validate — e.g. a nested model. Raised at declaration (app/workflow subclass
    creation) so a bad settings shape fails loudly where it's written, not at the first
    operator PATCH."""


class SubscriberDeclarationError(Exception):
    """A subscriber's signature asks for a routing key — one a filter matches on but
    no body is handed. Raised at declaration, so it fails on import instead of
    inside the durable step that publishes the signal."""


class AppLoadError(Exception):
    """An app could not be loaded, headlessly or at full boot. The concrete
    subclass names the failed stage; nothing raises this base directly."""


class AppNotFound(AppLoadError):
    """No installed app declares the requested name under the
    ``druks.apps`` entry-point group — the package isn't installed."""


class MalformedApp(AppLoadError):
    """The app's entry point resolves to something that isn't an
    ``App`` subclass, or its metadata target can't be resolved at all —
    a packaging mistake, not a runtime error inside the app."""


class AppImportError(AppLoadError):
    """Importing the app's models or capability modules raised. The
    app is installed and well-declared, but its own code failed on
    import — carries the original exception as its cause."""


class AppRouteConflict(AppLoadError):
    """An app mounts a router on a segment the platform serves for every app.
    Raised when the routers are enumerated, so the conflict fails the load
    instead of hiding one route behind the other."""


class AppSubjectContractError(AppLoadError):
    """A declared subject fails the read-side contract: no ``list_summaries()``
    implementation, or it names the reserved ``transcripts`` segment."""
