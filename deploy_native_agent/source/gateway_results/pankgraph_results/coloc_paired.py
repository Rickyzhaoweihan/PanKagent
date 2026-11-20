"""Read release-paired membership by exact coloc edge, role and source set."""

async def read_members(page, release, record, properties, role, limit):
    edge, snapshot = properties["id"], properties["snapshot_id"]
    expected_set = record["gwas_credible_set_id" if role == "gwas" else "qtl_signal_id"]
    owner = record["disease_id" if role == "gwas" else "gene_id"]
    request = {"release_id": release, "ref": {"kind": "edge", "type_id": "SIGNAL_COLOC_WITH__Gene__disease", "id": edge},
               "table_id": "credible_members", "filters": {"signal_role": role}, "limit": 100,
               "fields": ["signal_role", "variant_id", "credible_set_id", "pip", "p_value", "effect_allele", "other_allele", "beta", "lbf"]}
    rows, cursors, variants = [], set(), set()
    metadata = None
    while True:
        result = await page(request)
        if (result["state"], result["release_id"], result["object_id"], result["snapshot_id"]) != (
                "available", release, edge, snapshot):
            raise ValueError("Mismatched paired evidence")
        for row in result["records"]:
            info = result["credible_sets"][row["credible_set_id"]]
            if (row["signal_role"], row["snapshot_id"], row["object_id"], info["source_set"], info["object_id"]) != (
                    role, snapshot, owner, expected_set, owner):
                raise ValueError("Mismatched credible set")
            if metadata is not None and info != metadata:
                raise ValueError("Inconsistent credible-set metadata")
            metadata = info
            if row["variant_id"] in variants or len(rows) >= limit:
                raise ValueError("Duplicate or oversized credible set")
            variants.add(row["variant_id"])
            rows.append({**row, "snp": row["variant_id"], "slope": row["beta"]})
        if result["has_more"] is False:
            break
        cursor = result["next_cursor"]
        if not cursor or cursor in cursors or not result["records"]:
            raise ValueError("Invalid membership cursor")
        cursors.add(cursor)
        request["cursor"] = cursor
    if metadata is None or len(rows) != metadata["n_members"]:
        raise ValueError("Incomplete credible set")
    return rows, {"label": metadata["description"], "status": "available", "url": metadata["data_source_url"],
                  "source_version": snapshot, "source_completeness": metadata["completeness"],
                  "coverage": {"scope": "credible_set", "complete": True, "returned": len(rows)}}, []
