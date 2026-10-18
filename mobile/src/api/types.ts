/**
 * The wire contract with /api/v1 (see ../../../api_v1.py).
 *
 * These types are hand-mirrored from the Python side — `models.Listing` and
 * `store._row_from_listing` for rows, `verticals.Vertical.schema()` for the
 * form description. Two rules keep that from rotting:
 *
 *  1. Everything the server may omit is optional here. The backend fills
 *     unknown fields with "" or null rather than dropping them, but a client
 *     that assumes presence breaks the day that changes.
 *  2. Nothing is decoded structurally at runtime beyond what a screen reads.
 *     An unknown extra key from a newer server is inert, which is what lets
 *     the backend ship ahead of the app — the normal state of affairs once
 *     binaries are in the wild.
 */

export type VerticalId = 'guns' | 'parts' | 'ammo';

export interface VerticalRef {
  id: VerticalId;
  label: string;
  path: string;
}

/** GET /api/v1/meta — the single boot call. */
export interface Meta {
  api_version: number;
  min_client_version: string;
  verticals: VerticalRef[];
  features: {
    /** May the app show/open a link to the retailer's listing page? Server
     *  owned, because it tracks app-store policy, not app capability. */
    listing_links: boolean;
    stats: boolean;
    ballistics: boolean;
    /** Cloudflare-fronted sources only answer while the operator's worker is
     *  connected; false means those sites will report unavailable. */
    worker_online: boolean;
  };
}

// ---- search form, described by the server -------------------------------
// The form is not hardcoded in the app: /schema describes the inputs for each
// vertical and the app renders them. Adding a filter server-side reaches
// every installed build without a release.

export type FieldType = 'text' | 'select' | 'combo' | 'range' | 'checkbox';

export interface SelectOption {
  value: string;
  label: string;
}

export interface InputField {
  id: string;
  label: string;
  type: FieldType;
  presets?: string | null;
  placeholder?: string;
  default?: string | number | boolean | null;
  /** Server sends either objects or bare strings; both are handled. */
  options?: (SelectOption | string)[] | null;
  min_id?: string | null;
  max_id?: string | null;
  min_placeholder?: string;
  max_placeholder?: string;
  num_presets?: string[] | null;
}

export interface ColumnSpec {
  id: string;
  label: string;
  sortable?: boolean;
  render?: string;
  grid?: string;
}

export interface VerticalSchema {
  id: VerticalId;
  label: string;
  path: string;
  inputs: InputField[];
  columns: ColumnSpec[];
  presets: Record<string, string[]>;
  defaults: {
    sort: { key: string; dir: number };
    view: string;
  };
}

export interface SiteRef {
  name: string;
  label: string;
  homepage: string;
}

// ---- search state -------------------------------------------------------

/** Per-site progress. Mirrors the status pills in the web UI. */
export type ClientStatus =
  | 'queued'
  | 'running'
  | 'done'
  | 'blocked'
  | 'schema'
  | 'error'
  | 'unavailable';

export interface ClientState {
  site: string;
  status: ClientStatus;
  message: string;
  found: number;
}

/** One result row. Optional everywhere the source site may not say. */
export interface Listing {
  id: number;
  vertical: VerticalId;
  site: string;
  /** Empty string when the server's listing-links flag is off — the URL is
   *  withheld server-side, so this is absence, not a rendering choice. */
  url: string;
  title: string;
  manufacturer?: string;
  model?: string;
  caliber?: string;
  caliber_canon?: string;
  action?: string;
  gun_type?: string;
  kind?: string;
  barrel_length?: number | null;
  capacity?: string;
  capacity_rounds?: number | null;
  condition?: string;
  condition_grade?: string;
  /** 0/1 from Python, not booleans. */
  trade_in?: number;
  is_bundle?: number;
  grain?: number | null;
  bullet_type?: string;
  round_count?: number | null;
  price_per_round?: number | null;
  part_category?: string;
  in_stock?: number | null;
  listing_type?: string;
  /** Firm / buy-now price only. Never a live auction bid — the backend keeps
   *  those apart on purpose, and so must any UI that prints a price. */
  price?: number | null;
  current_bid?: number | null;
  bid_count?: number | null;
  ends_at?: number | null;
  posted_at?: number | null;
  image?: string;
  /** The stats engine has never observed this listing before. */
  is_new?: boolean;
}

export interface SearchState {
  id: number;
  status: 'running' | 'done';
  done: boolean;
  criteria: Record<string, unknown>;
  clients: ClientState[];
  /** Only rows newer than the `after` cursor sent with the request. */
  listings: Listing[];
}

export interface StartSearchResponse {
  search_id: number;
  poll_after_ms: number;
}

// ---- market stats -------------------------------------------------------
// Loosely typed on purpose: /stats returns a group-by shaped payload whose
// dimensions vary per vertical, and the screen renders whatever groups come
// back rather than encoding a fixed list the server would outgrow.

/** Price percentiles, as returned by `stats._price_summary`. `priced` is the
 *  number of listings that carried a firm price — always smaller than the
 *  group's `count`, because live auction bids are excluded from price stats
 *  and unpriced listings exist. Printing a median without it is misleading. */
export interface PriceSummary {
  priced: number;
  min: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  p95: number | null;
  max: number | null;
  mean: number | null;
}

export interface StatsGroupRow extends PriceSummary {
  key: string;
  count: number;
  /** Percent of matched listings in this group. */
  share: number;
}

export interface StatsResponse {
  vertical: VerticalId;
  group_by: string;
  group_choices: string[];
  /** "Price" for guns/parts, "cost per round" for ammo — the unit differs per
   *  vertical, so the label comes from the server rather than the screen. */
  price_label: string;
  total_stored: number;
  matched: number;
  overall: PriceSummary;
  groups: StatsGroupRow[];
  truncated_groups: number;
  bundles_excluded_from_prices: number;
  quality: Record<string, number>;
  quality_labels: Record<string, string>;
  version: number;
  as_of: number;
  /** Present instead of the rest when group_by isn't valid for the vertical. */
  error?: string;
  choices?: string[];
}
