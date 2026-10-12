/* 与 reports.same_dataset 同口径：plan_id 或旧根/阈值/排除三元组。 */
export function datasetKey(snap) {
  const plan = snap.plan_id == null ? "" : String(snap.plan_id);
  if (plan !== "") return JSON.stringify(["plan", plan]);
  const root = snap.root == null ? "" : String(snap.root);
  const minKb = snap.min_kb == null ? "" : String(snap.min_kb);
  const exclude = snap.exclude_names == null ? "" : String(snap.exclude_names);
  return JSON.stringify(["legacy", root, minKb, exclude]);
}
