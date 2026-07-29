# Parsed Document Viewer

This is a small Databricks App for checking the output of `ai_parse_document` against
the original PDF. You open a document on the left, the parsed elements for that same
page show up on the right, and you page through to see how well the parse held up.

## What you're looking at

The screen is split in two. On the left is the PDF, rendered page by page in the
browser. On the right are the elements `ai_parse_document` produced for that page: each
one tagged with its type (text, table, page header, footnote, figure, and so on).
Tables are drawn as tables rather than dumped as raw HTML.

Each side has its own set of dropdowns, so the PDF and the parsed table don't have to
live in the same place. The left dropdowns pick the PDF — catalog, schema, volume, then
the file. The right dropdowns pick the parsed output — catalog, schema, table, then
(if the table holds more than one document) which document to line up against the PDF.
In practice the PDFs are often in one volume and the parsed table in a different schema,
which is why the two browsers are independent. The page control is shared: turn a page
and both sides move together.

Two checkboxes at the top of the right panel change what you see:

- **show raw JSON (per element)** adds a collapsible block under each element with exactly
  what the parser returned for it — bounding box, confidence, type, the lot.
- **show raw page** replaces the element cards with the raw JSON for the whole page in one
  block, which is handy for copying out or diffing.

They're mutually exclusive — turning one on switches the other off.

## Before you start

You'll need three things:

- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) installed, and a
  profile pointing at the workspace you want to deploy into:

  ```bash
  databricks auth login --host https://<your-workspace-host>
  ```

  This prompts you for a **profile name** (it suggests one based on the host — press Enter
  to accept, or type your own, e.g. `banking-demo`). Remember whatever name you choose:
  every command below takes `--profile <your-profile>`, shown as `<PROFILE>` throughout.
- A SQL warehouse. Any size is fine, serverless included. The app uses it to list your
  catalogs/volumes/tables and to read the parsed rows — it's not doing heavy compute.
- A parsed table to point at. If you've run the `ai_parse_document_eval` notebook, the
  `..._elements` table it writes is exactly the shape this app expects (see below).

## Running it

From this folder. Grab your warehouse id first (SQL Warehouses > your warehouse >
Connection details). Both `--profile` and `--var="warehouse_id=..."` go on **both**
commands — the bundle re-reads them each time. Replace `<PROFILE>` with the profile name
you chose during `auth login`:

```bash
databricks bundle deploy -t dev --profile <PROFILE> --var="warehouse_id=<YOUR_WAREHOUSE_ID>"
databricks bundle run parsed_doc_viewer -t dev --profile <PROFILE> --var="warehouse_id=<YOUR_WAREHOUSE_ID>"
```

If you'd rather not repeat `--profile` on every command, export it once for the session
and drop the flag:

```bash
export DATABRICKS_CONFIG_PROFILE=<PROFILE>
databricks bundle deploy -t dev --var="warehouse_id=<YOUR_WAREHOUSE_ID>"
databricks bundle run parsed_doc_viewer -t dev --var="warehouse_id=<YOUR_WAREHOUSE_ID>"
```

The first command uploads the app and wires it to your warehouse. The second one starts
it and prints the URL — open that in a browser and you're in. Starting the app the first
time takes a couple of minutes while it provisions.

(`warehouse_id` has no default on purpose. If you forget it, the bundle stops with a clear
error rather than deploying an app that can't reach a warehouse. And if you skip the
profile entirely, you'll see a "cannot configure default credentials" error — that just
means the CLI doesn't know which workspace to use.)

One thing to sort out once, in the workspace: the app runs under its own service
principal, so that principal needs permission to see your data. If the dropdowns come up
empty when you open the app, or you get a "permission denied" in the parsed panel, this
is almost always why.

Find the app's service principal id:

```bash
databricks apps get parsed-doc-viewer --profile <PROFILE> | grep service_principal_client_id
```

Then grant it read access. The PDFs and the parsed table can be in different places, so
grant on each (skip the second pair if they're in the same schema) - Run the grants on a notebook or from the catalog explorer:

```sql
-- where the parsed table lives
GRANT USE CATALOG ON CATALOG <catalog>                     TO `<sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema>            TO `<sp-client-id>`;
GRANT SELECT      ON TABLE   <catalog>.<schema>.<table>    TO `<sp-client-id>`;

-- where the PDFs live
GRANT USE CATALOG ON CATALOG <pdf_catalog>                 TO `<sp-client-id>`;
GRANT USE SCHEMA  ON SCHEMA  <pdf_catalog>.<pdf_schema>    TO `<sp-client-id>`;
GRANT READ VOLUME ON VOLUME  <pdf_catalog>.<pdf_schema>.<volume> TO `<sp-client-id>`;
```

You can do the same thing from Catalog Explorer under Permissions if you'd rather click
through it.

## The parsed table it reads

The app expects the table to have one row per parsed element, with these columns:

| column | type | what it is |
|---|---|---|
| `document` | STRING | the source file name, e.g. `fidelity` |
| `page` | INT | page number, starting at 1 |
| `seq` | INT | order of the element within the page |
| `element_type` | STRING | `text`, `table`, `page_header`, `footnote`, `figure`, … |
| `content` | STRING | the element's content (tables come through as HTML) |
| `element` | VARIANT | optional — the full raw element, used by the raw-JSON toggle |

The `ai_parse_document_eval` notebook produces this automatically. When you open the
table dropdown, any table that matches this shape gets a ✓ next to it, so you don't have
to remember which one is which. If you pick a table that's missing one of the required
columns, the app will tell you which column it couldn't find rather than failing silently.

## Trying it locally first

If you want to poke at it on your laptop before deploying:

```bash
cd src
pip install -r requirements.txt
export DATABRICKS_PROFILE=<your CLI profile>
export DATABRICKS_WAREHOUSE_ID=<YOUR_WAREHOUSE_ID>
uvicorn app:app --reload --port 8000
```

Then open http://localhost:8000. It authenticates through your CLI profile, so you're
looking at the same data you'd see in the workspace.

## What's in here

```
parsed-doc-viewer/
├── databricks.yml          bundle definition (the app + its warehouse binding)
├── README.md
└── src/
    ├── app.py              backend: browses Unity Catalog, streams the PDF, reads parsed rows
    ├── app.yaml            how the app starts (the uvicorn command)
    ├── requirements.txt    four Python packages; the frontend has no build step
    └── static/
        ├── index.html      the whole UI — plain JavaScript, no build step
        └── vendor/         pdf.js, served by the app itself (no external dependency)
```

## Worth knowing

- The app is fully self-contained: pdf.js is bundled under `src/static/vendor/` and served
  by the app, so nothing is fetched from the internet at runtime. It works the same on an
  air-gapped or network-restricted workspace.
- Each PDF is held in the app's memory after the first load, which keeps page turns
  quick. Very large PDFs, or a lot of them in one session, will grow that footprint.
- Page numbers line up between the two panels even for documents over 500 pages.
  `ai_parse_document` has to parse those in slices, but it numbers the pages against the
  whole document rather than each slice, so the stitched table matches the PDF one to one.
```
