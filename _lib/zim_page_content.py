"""Shared helper for inserting new content into a Zim wiki page's text.

Used both by GoogletasksController.fetch() (googletasks.py) when importing new
Google tasks, and by insert_note.py when inserting arbitrary system text, so the
"where does new content go" rule is defined and tested in exactly one place.
"""


def insert_after_first_blank_line(lines, text):
    """Insert `text` as a new line right after the first blank line in `lines`.

    `lines` is expected to be already-dumped page content (e.g. from
    Page.dump('wiki')), which never includes the page's own file header
    (Content-Type/Wiki-Format) since Zim parses that out as metadata
    separately. If there is no blank line, `text` is appended at the end
    instead.
    """
    contents = []
    appended = False
    for line in lines:
        contents.append(line)
        if line.strip() == "" and not appended:
            appended = True
            contents.append(text)
    if not appended:
        contents.append(text)
    return contents
