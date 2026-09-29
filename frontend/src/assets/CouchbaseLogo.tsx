/**
 * The Couchbase mark, drawn exactly as the Couchbase Agent Operations Manager
 * draws it.
 *
 * `CouchbaseGlyph` is the white "cup" from the logo on the 24x24 grid the full
 * mark uses, so dropping it into the red brand square puts the cup at the size
 * and position it has inside the official red circle. It inherits
 * `currentColor`, so the container decides the colour.
 *
 * `CouchbaseBadge` is the complete mark -- red disc, white cup -- for places
 * where the logo stands on its own (the favicon uses the same drawing).
 *
 * `BrandMark` is the sidebar's rounded red tile with the cup inside.
 */

/** The cup, on the logo's own 24x24 grid. */
const CUP_PATH =
  "M20.111 14.104a1.467 1.458 0 0 1-1.235 1.503c-1.422.244-4.385.398-6.875.398" +
  "s-5.454-.15-6.877-.398c-.814-.14-1.235-.787-1.235-1.503V9.417a1.57 1.56 0 0 1 " +
  "1.235-1.505 15.72 15.619 0 0 1 2.156-.14.537.533 0 0 1 .523.543v3.303c1.463 0 " +
  "2.727-.086 4.201-.086 1.474 0 2.727.086 4.196.086V8.342a.535.532 0 0 1 .494-.569h.027" +
  "a15.995 15.891 0 0 1 2.156.14 1.57 1.56 0 0 1 1.234 1.504z";

export const COUCHBASE_RED = "#ea2328";

export function CouchbaseGlyph({ title = "Couchbase" }: { title?: string }) {
  return (
    <svg viewBox="0 0 24 24" role="img" aria-label={title} focusable="false" className="couchbase-glyph">
      <path d={CUP_PATH} fill="currentColor" />
    </svg>
  );
}

export function CouchbaseBadge({ size = 34, title = "Couchbase" }: { size?: number; title?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-label={title} focusable="false">
      <circle cx="12" cy="12" r="12" fill={COUCHBASE_RED} />
      <path d={CUP_PATH} fill="#fff" />
    </svg>
  );
}

export function BrandMark({ title }: { title?: string }) {
  return (
    <div className="brand-mark">
      <CouchbaseGlyph title={title} />
    </div>
  );
}
