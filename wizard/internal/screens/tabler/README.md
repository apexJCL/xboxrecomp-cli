# Tabler glyphs

Outline glyphs of Tabler Icons 3.34.1 (<https://tabler.io/icons>), each
copied as it is from
`https://unpkg.com/@tabler/icons@3.34.1/icons/outline/<name>.svg`. MIT
licence: `LICENSES/tabler-icons-MIT.txt` at the repository's root.

`icons.go` embeds them and puts the theme's colour in for `currentColor`
when Fyne reads one (Fyne's own themed resources recolour fills only, and
these are strokes).

| Glyph | Where |
|---|---|
| `external-link` | after every hint link and credit: it opens the browser |
| `info-circle` | the header's button that opens the open-source credits |
| `download`, `photo`, `list-check`, `settings`, `code`, `hammer`, `package` | the Building checklist's part kinds |
| `circle-dashed`, `circle-check`, `circle-x`, `circle-minus`, `player-stop` | the checklist's states |
