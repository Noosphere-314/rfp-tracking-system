-- Launch configuration (System-Design.md §6, Deployment-Plan Stage 2.2).
-- Stage 2 goes live with five Discourse forums + Snapshot + RSS enabled.
-- The remaining Tier-1 forums are inserted disabled: Stage 3 flips them on
-- from the admin form, which is also the rehearsal for adding new ones.

-- ── settings ───────────────────────────────────────────────────────
INSERT INTO settings (key, value) VALUES
    -- Dual threshold (A8): auto-deliver above, review band below.
    ('confidence_threshold',   '0.7'),
    ('review_band_low',        '0.4'),
    ('alert_channel',          '#rfp-alerts'),
    ('review_channel',         '#rfp-review'),
    ('digest_channel',         '#rfp-digest'),
    ('lead_title_template',    '[{label}] {ecosystem} — {title}'),
    -- Volume guard: above this, the top N by confidence become leads and the
    -- rest arrive as one digest message. Inbox trust is the thing being protected.
    ('max_leads_per_run',      '25'),
    -- Lead-floor heartbeat (A3): fewer than this in 7 days means something
    -- upstream is broken, even though nothing has thrown an error.
    ('lead_floor_7d',          '3'),
    -- Source-went-dark alert (A3).
    ('source_dark_days',       '14'),
    ('classifier_prompt_version', 'v1')
ON CONFLICT (key) DO NOTHING;

-- ── keywords ───────────────────────────────────────────────────────
-- Deliberately wide: this filter only has to be cheaper than Claude, and a
-- false positive costs a fraction of a cent while a false negative costs a
-- $500k RFP. Tightening happens after the calibration run, from the form.
INSERT INTO keywords (pattern, kind) VALUES
    ('\bRFPs?\b',                                          'include'),
    ('request for (proposals?|quotations?|comments?)',      'include'),
    ('\b(grant|grants|grantee|funding round)\b',            'include'),
    ('\b(bounty|bounties)\b',                               'include'),
    ('\b(procurement|vendor|contractor|service provider)\b', 'include'),
    ('\b(proposal|budget request|treasury (request|allocation))\b', 'include'),
    ('\b(seeking|looking for|call for) (a )?(devs?|developers?|teams?|builders?|proposals?|applications?)\b', 'include'),
    ('\b(mission request|intent|retro(active)? funding|retropgf)\b', 'include'),
    ('\b(scope of work|statement of work|deliverables)\b',  'include'),
    ('\b(audit|integration|tooling|infrastructure) (rfp|request|proposal)\b', 'include')
ON CONFLICT (pattern, kind) DO NOTHING;

-- Funding-lane extraction from general crypto news (RSS): a story only reaches
-- the classifier if it looks like a capital event. The classifier + the
-- funding lane's higher bar (A8) do the precision work.
INSERT INTO keywords (pattern, kind) VALUES
    ('\b(raises?|raised|closes?|closed|secures?|secured) \$?\d+(\.\d+)?\s?(m|million|b|billion)\b', 'include'),
    ('\b(funding|seed|strategic|series [a-d]) round\b',     'include'),
    ('\bseries [a-d]\b',                                    'include'),
    ('\b(venture (capital|arm|fund)|treasury diversification)\b', 'include')
ON CONFLICT (pattern, kind) DO NOTHING;

INSERT INTO keywords (pattern, kind) VALUES
    -- Recurring forum furniture that matches the include list but never is one.
    ('\b(weekly|monthly) (recap|digest|update|newsletter)\b', 'exclude'),
    ('\b(meeting|call) (notes|minutes|recording)\b',          'exclude'),
    ('\b(airdrop|giveaway|whitelist|presale)\b',              'exclude'),
    ('\b(grantee (report|update)|milestone report)\b',        'exclude')
ON CONFLICT (pattern, kind) DO NOTHING;

-- ── Tier 1: Discourse governance forums ────────────────────────────
-- category = the human-readable label; config.categories carries the
-- {slug, id} pairs the fetcher needs for /c/<slug>/<id>.json.
INSERT INTO sources (type, name, ecosystem, url, category, config, enabled) VALUES
    ('discourse', 'Arbitrum DAO forum', 'Arbitrum',
     'https://forum.arbitrum.foundation', 'grants + procurement',
     '{"categories": [{"slug": "dao-grant-programs", "id": 16},
                      {"slug": "procurement-nominations", "id": 44}]}', true),

    ('discourse', 'Solana forum', 'Solana',
     'https://forum.solana.com', 'RFP',
     '{"categories": [{"slug": "rfp", "id": 10}]}', true),

    -- gov-fund-missions is a subcategory of grants; the parent listing already
    -- includes its topics, and the fetcher dedups on bare topic id anyway.
    ('discourse', 'Optimism governance', 'Optimism',
     'https://gov.optimism.io', 'grants + missions',
     '{"categories": [{"slug": "grants", "id": 87},
                      {"slug": "grants/gov-fund-missions", "id": 69}]}', true),

    ('discourse', 'Avalanche forum', 'Avalanche',
     'https://forum.avax.network', 'grants + infraBUIDL',
     '{"categories": [{"slug": "avalanche-grants", "id": 11},
                      {"slug": "infrabuidl", "id": 13}]}', true),

    ('discourse', 'Lido research', 'Lido',
     'https://research.lido.fi', 'community grants',
     '{"categories": [{"slug": "community-grants", "id": 13}]}', true),

    -- Stage 3 — enabled from the admin form, not from a deploy.
    ('discourse', 'dYdX forum', 'dYdX',
     'https://dydx.forum', 'grants',
     '{"categories": [{"slug": "grants", "id": 14}]}', false),

    ('discourse', 'ENS forum', 'ENS',
     'https://discuss.ens.domains', 'public goods',
     '{"categories": [{"slug": "public-goods", "id": 37}]}', false),

    -- NB: migrated from forum.safe.global — the old domain is not a fallback.
    ('discourse', 'Safe forum', 'Safe',
     'https://forum.safefoundation.org', 'grants',
     '{"categories": [{"slug": "grants", "id": 43}]}', false),

    ('discourse', 'Polygon forum', 'Polygon',
     'https://forum.polygon.technology', 'community treasury',
     '{"categories": [{"slug": "community-treasury", "id": 94}]}', false),

    ('discourse', 'Uniswap governance', 'Uniswap',
     'https://gov.uniswap.org', 'service providers',
     '{"categories": [{"slug": "service-providers", "id": 14}]}', false)
ON CONFLICT (type, url, category) DO NOTHING;

-- ── Tier 2: Snapshot (one source, curated space allowlist) ─────────
INSERT INTO sources (type, name, ecosystem, url, category, config, enabled) VALUES
    ('snapshot', 'Snapshot proposals', 'multi',
     'https://hub.snapshot.org/graphql', 'curated spaces',
     '{"spaces": ["opcollective.eth", "arbitrumfoundation.eth",
                  "uniswapgovernance.eth", "ens.eth", "safe.eth",
                  "lido-snapshot.eth"]}', true)
ON CONFLICT (type, url, category) DO NOTHING;

-- ── Tier 3: funding-signal RSS ─────────────────────────────────────
-- Separate lane (A8): a funding signal is a BD heuristic, not an RFP, and is
-- held to its own higher bar so it cannot dilute RFP-lane precision.
-- URLs verified 2026-07-30: coindesk redirects to the no-trailing-slash form,
-- blockworks moved from .co to .com.
INSERT INTO sources (type, name, ecosystem, url, category, config, lane, enabled) VALUES
    ('rss', 'TechCrunch Crypto', 'multi',
     'https://techcrunch.com/category/cryptocurrency/feed/', 'funding news',
     '{}', 'funding', true),
    ('rss', 'CoinDesk', 'multi',
     'https://www.coindesk.com/arc/outboundfeeds/rss', 'funding news',
     '{}', 'funding', true),
    ('rss', 'Blockworks', 'multi',
     'https://blockworks.com/feed', 'funding news',
     '{}', 'funding', true)
ON CONFLICT (type, url, category) DO NOTHING;

-- ── Tier 2: REST aggregators ───────────────────────────────────────
-- DAOstar OpenGrants: open grant pools across Octant/Giveth/Stellar/…
-- (verified 2026-07-30: /api/v1/grantPools returns DAOIP-5 pools with isOpen).
INSERT INTO sources (type, name, ecosystem, url, category, config, enabled) VALUES
    ('rest_aggregator', 'DAOstar OpenGrants pools', 'multi',
     'https://grants.daostar.org/api/v1/grantPools', 'open grant pools',
     '{"params": {"limit": "100"},
       "items_path": "data",
       "fields": {"external_id": "id", "title": "name", "url": "governanceURI",
                  "body": "description", "ts": "closeDate"},
       "require": {"isOpen": true},
       "url_base": "https://grants.daostar.org"}', true),

    -- Karma GAP lists *awarded* grants (funding history, not open requests) —
    -- valuable for the Stage-5 knowledge base, wrong shape for the lead lane.
    -- Kept disabled until that decision is revisited with real data.
    ('rest_aggregator', 'Karma GAP (Arbitrum grants)', 'Arbitrum',
     'https://gapapi.karmahq.xyz/communities/arbitrum/grants', 'awarded grants',
     '{"params": {"page": "0", "pageLimit": "50"},
       "items_path": "data",
       "fields": {"external_id": "uid", "title": "details.title",
                  "url": "details.proposalURL", "body": "details.description",
                  "ts": "createdAt"}}', false)
ON CONFLICT (type, url, category) DO NOTHING;

-- ── Tier 3: TVL spikes (FUNDING lane) ──────────────────────────────
INSERT INTO sources (type, name, ecosystem, url, category, config, lane, enabled) VALUES
    -- prequalified: items are already threshold-gated by the fetcher itself;
    -- the RFP keyword pre-filter does not apply to them.
    ('defillama', 'DefiLlama TVL spikes', 'multi',
     'https://api.llama.fi/protocols', 'tvl momentum',
     '{"min_tvl": 1000000, "min_change_7d": 50, "max_items": 15, "prequalified": true}',
     'funding', true)
ON CONFLICT (type, url, category) DO NOTHING;

-- ── Stage 4: GitHub Discussions (needs GITHUB_TOKEN in worker env) ─
INSERT INTO sources (type, name, ecosystem, url, category, config, enabled) VALUES
    ('github_discussions', 'Filecoin community discussions', 'Filecoin',
     'https://github.com/filecoin-project/community', 'discussions',
     '{"repos": [{"owner": "filecoin-project", "name": "community"}]}', false),
    ('github_discussions', 'TON grants and bounties', 'TON',
     'https://github.com/ton-society/grants-and-bounties', 'discussions',
     '{"repos": [{"owner": "ton-society", "name": "grants-and-bounties"}]}', false)
ON CONFLICT (type, url, category) DO NOTHING;
