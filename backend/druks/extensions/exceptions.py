class ExtensionConfigError(Exception):
    """An extension's ``.druks`` config could not be parsed or validated.

    Raised at intake (and by push-time validation) so bad config fails
    loudly where the work starts instead of half-applying."""


class SettingsDeclarationError(Exception):
    """A ``Settings`` inner class declares a field the settings plane can't render or
    validate — e.g. a nested model. Raised at declaration (extension/workflow subclass
    creation) so a bad settings shape fails loudly where it's written, not at the first
    operator PATCH."""


class SubscriberDeclarationError(Exception):
    """A subscriber's signature asks for a routing key — one a filter matches on but
    no body is handed. Raised at declaration, so it fails on import instead of
    inside the durable step that publishes the signal."""


class ExtensionLoadError(Exception):
    """An extension could not be loaded — whether app-lessly or during full API
    boot. The concrete subclass names which stage failed — nothing raises this
    base directly."""


class ExtensionNotFound(ExtensionLoadError):
    """No installed extension declares the requested name under the
    ``druks.extensions`` entry-point group — the package isn't installed."""


class MalformedExtension(ExtensionLoadError):
    """The extension's entry point resolves to something that isn't an
    ``Extension`` subclass, or its metadata target can't be resolved at all —
    a packaging mistake, not a runtime error inside the extension."""


class ExtensionImportError(ExtensionLoadError):
    """Importing the extension's models or capability modules raised. The
    extension is installed and well-declared, but its own code failed on
    import — carries the original exception as its cause."""


class ExtensionSubjectContractError(ExtensionLoadError):
    """A subject a workflow declares does not satisfy the read-side contract the
    platform will call on it — it resolves ``list_summaries()`` to the platform
    stub instead of a real implementation, or it names the reserved
    ``transcripts`` segment. Raised at load (full boot and app-less alike), so a
    subject that would 500 the board on first click stops the extension from
    loading instead."""
