// Import every bundled app's UI module for its registration side effect. An
// app contributes frontend by calling ``registerAppUI`` at import time;
// listing it here is what pulls it into the bundle. Adding an app's UI is one
// line here — the shell (App, AppDropdown, api/client) never learns its name.
import './software_factory/ui'
import './review/ui'
