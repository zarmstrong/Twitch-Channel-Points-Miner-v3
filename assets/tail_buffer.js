// Shared by the dashboard's Logs tab (assets/script.js) and the Windows
// shell's Console tab (assets/windows_shell.html, inlined there by
// windows_launcher.py since that window loads its HTML as a raw string with
// no base URL a <script src> could resolve against).
//
// Both tabs poll for new text forever and used to append every chunk to an
// ever-growing string with no upper bound, so a long-running session (either
// tab keeps polling in the background even when not the active one)
// accumulated the whole run's worth of text - megabytes of retained content,
// and on Chromium/WebView2, gigabytes of process memory after several hours.
// This keeps only the tail, the same way the server already bounds a single
// response (see MAX_LOG_TAIL_BYTES).
function createTailBuffer(maxChars) {
    var text = '';
    return {
        append: function (chunk) {
            text += chunk;
            if (text.length > maxChars) {
                text = text.slice(text.length - maxChars);
            }
            return text;
        },
        get: function () {
            return text;
        }
    };
}
