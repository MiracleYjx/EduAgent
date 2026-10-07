from benchmark.t189.fault_acceptance import actual_failure_step


def test_check_heading_cannot_prove_the_expected_fault():
    messages = "检查：PostgreSQL / pgvector / Redis\n启动失败：[PostgreSQL / pgvector] DatabaseNotReadyError；下一步：启动数据库\n"
    assert actual_failure_step(messages) == "PostgreSQL / pgvector"
    assert actual_failure_step(messages) != "Redis"


def test_only_explicit_failure_step_is_observed():
    assert actual_failure_step("检查：Redis\n通过：Redis\n") is None
    assert (
        actual_failure_step(
            "检查：Redis\n启动失败：[Redis] RedisNotReadyError；下一步：启动 Redis\n"
        )
        == "Redis"
    )
