// ISS-100 失败探针（临时提交，随后 revert，不入交付）：注入必然失败，
// 证明 cargo test 失败使 cargo job 变红、退出码不被 shell/管道吞掉。
#[test]
fn iss100_probe_injected_failure() {
    assert!(false, "ISS-100 失败探针：注入的必然失败");
}
