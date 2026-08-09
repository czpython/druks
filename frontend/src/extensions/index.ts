// Import every bundled extension's UI module for its registration side effect. An
// extension contributes frontend by calling ``registerExtensionUI`` at import time;
// listing it here is what pulls it into the bundle. Adding an extension's UI is one
// line here — the shell (App, ExtensionDropdown, api/client) never learns its name.
import './ship/ui'
import './review/ui'
