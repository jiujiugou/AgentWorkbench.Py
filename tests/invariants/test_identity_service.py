# ruff: noqa: S105, S106 —— 本文件里的口令全部是测试值（`PASSWORD` 常量），
# 不连任何真实账号。逐处 noqa 会有十几条，反而让「哪一处是真凭据」看不出来。
"""编排层的不变量：登录四步、失败计数、三条凭据变更路径、邀请、引导。

这一层测的是**顺序与事务边界**，不是 SQL 也不是判定规则：

- 顺序：先跑哈希再分支（否则响应快慢成了账号枚举器）；先判邀请再建号
  （否则过期邀请会留下一个没有入口的账号）；
- 事务：改状态与吊销票据要么一起生效，要么都不生效（否则「改完密码别的设备
  还登着」，而每一半单独看都对）；
- 三条凭据变更路径的**差别**：改密保留当前这条、管理员重置一条不留、
  停用吊销全部且恢复后旧票不复活。三者混成一条实现时，测试仍然全绿——
  因为每条单独走一遍都不报错，差别只在「哪个 cookie 还能用」上。

改坏这里，用户会看到什么：

- `login` 在 `account is None` 时提前 return → 不存在账号比口令错快几十毫秒，
  一份邮箱列表就能筛出所有注册过的账号（`I-H2` 的耗时那半条）；
- 改密时把 `keep_ticket_id` 去掉 → 用户改完密码自己被踢下线，第一反应是
  「系统坏了」，然后去找管理员；
- 改密时保留票据却不刷新版本号 → 行还在，下一次请求照样 401。**这是最隐蔽的一种**：
  「保留」的代码写了、测试也断言了行数，但用户拿到的仍是「突然要我重新登录」；
- 锁定写进 `judge_access` → 别人拿你的邮箱连错几次就能把你挤下线（`I-V8`），
  成本比猜中密码低得多。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import load_script

from agentworkbench.config import AuthSettings, Settings
from agentworkbench.db import engine
from agentworkbench.identity import credentials, policy, service, store
from agentworkbench.identity.models import (
    AccountStatus,
    Outcome,
    ReasonCode,
    Role,
)

NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a totally different passphrase"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = engine.connect(tmp_path / "identity.db")
    engine.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def cfg() -> service.AuthConfig:
    """阈值调成 3，只为了让「达阈值」三次就能到。

    **生产默认值不在这里抄**——`AuthConfig` 的字段默认值从 `AuthSettings()`
    派生，抄一份的后果是测试覆盖的路径与生产的路径不是同一条。
    """
    return service.AuthConfig(login_max_failures=3, lockout_seconds=60, ticket_ttl_hours=12)


def seed_admin(conn: sqlite3.Connection, *, email: str = "admin@example.com") -> str:
    # 大多数用例测的是登录/改密/停用本身，不是首登改密；显式声明“这个管理员
    # 已经轮换过初始口令”，免得不相关的用例被闸门挡住。
    result = service.bootstrap(
        conn,
        email=email,
        password=PASSWORD,
        tenant_name="团队甲",
        now=NOW,
        must_change_password=False,
    )
    return result.user_id


def tenant_of(conn: sqlite3.Connection, user_id: str) -> str:
    account = store.get_user(conn, user_id)
    assert account is not None
    return account.tenant_id


def access(conn: sqlite3.Connection, ticket: str, *, now: datetime = NOW) -> policy.Decision:
    """把一张票据明文走一遍「判定四步」。**这是所有「还能不能用」断言的唯一入口**：

    直接看 `revoked_at` 只能证明「行变了」，证明不了「服务端会拒」——
    而用户感受到的是后者。
    """
    row = store.get_ticket_by_hash(conn, credentials.hash_ticket(ticket))
    account = None if row is None else store.get_user(conn, row.user_id)
    return policy.judge_access(ticket=row, account=account, now=now)


# ── 引导（I-V12） ──


def test_bootstrap_creates_a_loginable_admin(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """空库引导之后，那个人**真的能登录**，不是「库里有行」。

    判据从用户侧写：拿引导时给的口令走一遍登录，拿到票据，票据过判定。
    只断言「表里多了一行」的版本，在口令哈希写错（比如存了明文再校验明文）时也是绿的。
    """
    result = service.bootstrap(
        conn, email="Admin@Example.com", password=PASSWORD, tenant_name="团队甲", now=NOW
    )

    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)

    assert isinstance(logged, service.LoginSucceeded)
    assert logged.account.user_id == result.user_id
    assert logged.account.role is Role.ADMIN
    assert logged.account.tenant_id == result.tenant_id
    assert logged.account.must_change_password is True
    assert access(conn, logged.ticket).outcome is Outcome.ALLOW


def test_bootstrap_marks_the_admin_for_password_rotation(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """引导出来的管理员仍能登录，但账户上带着“先改初始口令”的标记。

    这条不是“引导成功”的重复：`bootstrap_creates_a_loginable_admin` 只证明
    口令对；本用例证明**生产路径**（默认参数）会把标记置 1。默认值写成 False
    时前者全绿，而首登强制改密整条链静默失效。
    """
    result = service.bootstrap(
        conn, email="admin@example.com", password=PASSWORD, tenant_name="团队甲", now=NOW
    )

    account = store.get_user(conn, result.user_id)
    assert account is not None
    assert account.must_change_password is True

    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)


def test_change_password_clears_the_rotation_flag(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """本人改密成功后标记必须清 0；只换哈希不清标记等于永远改不完。"""
    result = service.bootstrap(
        conn, email="admin@example.com", password=PASSWORD, tenant_name="团队甲", now=NOW
    )
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)

    changed = service.change_password(
        conn,
        user_id=result.user_id,
        current_ticket=logged.ticket,
        old_password=PASSWORD,
        new_password=NEW_PASSWORD,
        now=NOW,
    )

    assert changed is True
    account = store.get_user(conn, result.user_id)
    assert account is not None and account.must_change_password is False


def test_reset_password_marks_the_account_for_rotation(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """管理员重置是“已泄露/进不去”的处置，重置后的口令也只是一次性的。"""
    user_id = seed_admin(conn)

    service.reset_password(conn, user_id=user_id, new_password=NEW_PASSWORD, now=NOW)

    account = store.get_user(conn, user_id)
    assert account is not None and account.must_change_password is True
    assert isinstance(
        service.login(conn, email="admin@example.com", password=NEW_PASSWORD, now=NOW, config=cfg),
        service.LoginSucceeded,
    )


def test_change_password_refuses_reusing_the_same_password(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """新口令等于旧口令时不能清标记——否则“必须改密”可以靠原样提交绕过。"""
    result = service.bootstrap(
        conn, email="admin@example.com", password=PASSWORD, tenant_name="团队甲", now=NOW
    )
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)
    before = store.get_user(conn, result.user_id)
    assert before is not None

    with pytest.raises(service.PasswordUnchanged):
        service.change_password(
            conn,
            user_id=result.user_id,
            current_ticket=logged.ticket,
            old_password=PASSWORD,
            new_password=PASSWORD,
            now=NOW,
        )

    after = store.get_user(conn, result.user_id)
    assert after is not None
    assert after.must_change_password is True
    assert after.credential_epoch == before.credential_epoch


def test_bootstrap_refuses_a_second_run_and_changes_nothing(conn: sqlite3.Connection) -> None:
    """第二次引导必须失败，且**一行都不动**。

    `I-V12` 的措辞是「用户数与团队数都不变」。只看「抛了异常」不够：
    先建团队再判「已有用户」的实现同样会抛，但库里已经多了一个团队——
    而那个团队永远不会有人进去，只会出现在运维的列表里。
    """
    seed_admin(conn)
    users_before = store.count_users(conn)
    tenants_before = int(conn.execute("select count(*) as n from tenants").fetchone()["n"])

    with pytest.raises(service.BootstrapClosed):
        service.bootstrap(
            conn, email="second@example.com", password=PASSWORD, tenant_name="团队乙", now=NOW
        )

    assert store.count_users(conn) == users_before
    assert int(conn.execute("select count(*) as n from tenants").fetchone()["n"]) == tenants_before


def test_bootstrap_cli_exits_zero_and_leaves_a_working_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文档说的那条命令真的能跑通（`I-V12` / `I-H1` 的自动化部分）。

    判据的原文含「退出码 0」，所以这里调 `main()` 的返回值，不是调 service——
    一个「service 对了但脚本参数接错」的实现，用户拿到的仍然是启动失败。
    """
    module = load_script("bootstrap_identity")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AWB_BOOTSTRAP_PASSWORD", PASSWORD)

    code = module.main(["--email", "admin@example.com", "--tenant", "团队甲"])

    assert code == 0
    connection = engine.connect(tmp_path / "agentworkbench.db")
    try:
        account = store.get_user_by_email(connection, "admin@example.com")
        logged = service.login(
            connection,
            email="admin@example.com",
            password=PASSWORD,
            now=NOW,
            config=service.AuthConfig(),
        )
    finally:
        connection.close()
    assert account is not None and account.must_change_password is True
    assert isinstance(logged, service.LoginSucceeded)


def test_bootstrap_cli_second_run_is_nonzero_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退出码必须能区分「已经建过了」与「命令敲错了」——两种的下一步完全相反。"""
    module = load_script("bootstrap_identity")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AWB_BOOTSTRAP_PASSWORD", PASSWORD)
    assert module.main(["--email", "admin@example.com", "--tenant", "团队甲"]) == 0

    code = module.main(["--email", "other@example.com", "--tenant", "团队乙"])

    assert code != 0
    connection = engine.connect(tmp_path / "agentworkbench.db")
    try:
        assert store.count_users(connection) == 1
        assert int(connection.execute("select count(*) as n from tenants").fetchone()["n"]) == 1
    finally:
        connection.close()


# ── 登录（I-H2 的可自动化部分 / REQ-AUTH-07） ──


def test_unknown_email_still_runs_a_password_verification(
    conn: sqlite3.Connection, cfg: service.AuthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账号不存在时**必须仍然跑一次哈希**。

    断言的是机制而不是耗时：真耗时差异要人观察（`I-H2`），但机制可以对自动化断言。
    去掉这一步的实现功能完全正确、测试全绿，只是「不存在」会比「口令错」快几十毫秒——
    那几十毫秒就是一台不需要任何额外接口的账号枚举器。
    """
    seed_admin(conn)
    seen: list[str | None] = []
    real = credentials.verify_password

    def spy(password: str, hashed: str | None) -> tuple[bool, str | None]:
        seen.append(hashed)
        return real(password, hashed)

    monkeypatch.setattr(credentials, "verify_password", spy)

    result = service.login(conn, email="nobody@example.com", password=PASSWORD, now=NOW, config=cfg)

    assert isinstance(result, service.LoginRejected)
    assert seen == [None], "账号不存在时没有走校验路径（hashed 为 None 也应被调用）"


def test_wrong_password_is_rejected_without_naming_the_account(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """口令错与账号不存在返回**同一个原因码**。

    两个不同的 reason 只会躺在返回值里等着被谁拿去拼一句更具体的提示，
    而那正是对外唯一不能有的差别（`I-H2`）。
    """
    seed_admin(conn)

    wrong = service.login(
        conn, email="admin@example.com", password="not the password", now=NOW, config=cfg
    )
    missing = service.login(
        conn, email="nobody@example.com", password="not the password", now=NOW, config=cfg
    )

    assert isinstance(wrong, service.LoginRejected)
    assert isinstance(missing, service.LoginRejected)
    assert wrong.reason is missing.reason is ReasonCode.UNAUTHENTICATED
    # 只有记录里能区分：一个是「找到了人、口令不对」，一个是「根本没这个人」。
    assert wrong.actor_user_id is not None
    assert missing.actor_user_id is None


def test_failures_accumulate_and_lock_at_the_configured_threshold(conn: sqlite3.Connection) -> None:
    """锁定的阈值来自配置，不是常量。

    同一个账户在 `login_max_failures=3` 下第 3 次被锁；换一个阈值，锁的时机跟着变。
    断言「第 3 次」这个数本身没有意义——有意义的是它等于配置里那个数。
    """
    user_id = seed_admin(conn)
    strict = service.AuthConfig(login_max_failures=3, lockout_seconds=60, ticket_ttl_hours=12)

    for _ in range(2):
        result = service.login(
            conn, email="admin@example.com", password="wrong", now=NOW, config=strict
        )
        assert isinstance(result, service.LoginRejected)
        assert store.get_user(conn, user_id).lockout_until is None  # type: ignore[union-attr]

    service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=strict)

    account = store.get_user(conn, user_id)
    assert account is not None
    assert account.lockout_until == NOW + timedelta(seconds=60)
    assert account.status is AccountStatus.ACTIVE  # 锁定不碰状态
    assert account.failed_attempts == 0  # 达阈值后归零，解锁后重新有满额度


def test_locked_account_is_stopped_on_the_login_path_only(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V8`：别人连错几次，A 已登录的会话**仍然可用**。

    这是整条判据的要害：把锁定写进 `judge_access` 的实现，功能上「更安全」，
    代价是任何人拿别人邮箱乱试就能把他挤下线——成本比猜中密码低得多。
    """
    seed_admin(conn)
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)

    for _ in range(cfg.login_max_failures):
        service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=cfg)

    blocked = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)

    assert isinstance(blocked, service.LoginRejected)
    assert blocked.reason is ReasonCode.ACCOUNT_LOCKED_OUT
    # 已签发的会话照旧
    assert access(conn, logged.ticket, now=NOW + timedelta(minutes=5)).outcome is Outcome.ALLOW


def test_lockout_expires_by_clock_without_any_write(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """到期靠时间比较自愈，不靠一次「解锁」写操作。

    写成定时任务或登录时「发现过期就清」的实现也行，但那时「锁到什么时候」
    就有了两个真源：列里的值与清理代码的触发时机。这里只认时间。
    """
    user_id = seed_admin(conn)
    for _ in range(cfg.login_max_failures):
        service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=cfg)

    later = NOW + timedelta(seconds=cfg.lockout_seconds + 1)
    result = service.login(
        conn, email="admin@example.com", password=PASSWORD, now=later, config=cfg
    )

    assert isinstance(result, service.LoginSucceeded)
    account = store.get_user(conn, user_id)
    assert account is not None
    assert account.lockout_until is None
    assert account.failed_attempts == 0


def test_a_successful_login_resets_the_counter(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """两次错、一次对、再两次错——**不该被锁**。

    不归零的实现里，计数是终身累计的：用户三个月前错过两次，今天再错一次就被锁，
    而他没有任何办法知道为什么。
    """
    user_id = seed_admin(conn)
    for _ in range(2):
        service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=cfg)
    service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    for _ in range(2):
        service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=cfg)

    account = store.get_user(conn, user_id)
    assert account is not None
    assert account.lockout_until is None
    assert account.failed_attempts == 2


def test_logout_revokes_the_row_not_just_the_cookie(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V14`：登出之后同一张票据在服务端已经不可用。

    只清客户端的实现，把 cookie 手工带回来仍然 200——而「登出」对用户的承诺
    是「这台机器上别人拿不到了」。
    """
    seed_admin(conn)
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)

    assert service.logout(conn, ticket=logged.ticket, now=NOW) is True
    assert service.logout(conn, ticket=logged.ticket, now=NOW) is False  # 幂等

    row = store.get_ticket_by_hash(conn, credentials.hash_ticket(logged.ticket))
    assert row is not None
    assert row.revoked_at is not None
    assert access(conn, logged.ticket).outcome is Outcome.UNAUTHENTICATED


# ── 改密（I-V9） ──


def test_change_password_keeps_the_asking_session_and_kills_the_others(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """**这一格的核心断言**：cookie1 仍 200，cookie2 401。

    三件事缺一不可，而它们各自都能单独「看起来对」：

    - 吊销其余票据 → 不吊销的话 cookie2 活到过期，「改密」等于没改；
    - 保留发起改密的那条 → 全吊销的话用户改完自己被踢；
    - 把保留的那条**升到新一代**（`refresh_ticket_epoch`）→ 只保留行不升版本，
      下一次请求照样被整数比较拒掉。这一步最隐蔽：行数对了、`revoked_at` 也对，
      用户拿到的仍是「突然要我重新登录」。
    """
    user_id = seed_admin(conn)
    first = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    second = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(first, service.LoginSucceeded)
    assert isinstance(second, service.LoginSucceeded)

    assert (
        service.change_password(
            conn,
            user_id=user_id,
            current_ticket=first.ticket,
            old_password=PASSWORD,
            new_password=NEW_PASSWORD,
            now=NOW,
        )
        is True
    )

    assert access(conn, first.ticket).outcome is Outcome.ALLOW, "发起改密的那条被踢了"
    assert access(conn, second.ticket).outcome is Outcome.UNAUTHENTICATED, "别的会话还活着"

    second_row = store.get_ticket_by_hash(conn, credentials.hash_ticket(second.ticket))
    assert second_row is not None and second_row.revoked_at is not None, "只靠版本比较不算吊销"

    # 新口令可用、旧口令不可用
    assert isinstance(
        service.login(conn, email="admin@example.com", password=NEW_PASSWORD, now=NOW, config=cfg),
        service.LoginSucceeded,
    )
    assert isinstance(
        service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg),
        service.LoginRejected,
    )


def test_change_password_with_a_wrong_old_password_changes_nothing(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """旧口令错时**一个字节都不许变**。

    先换哈希再校验旧口令的实现，会让任何拿到已登录 cookie 的人直接改掉口令——
    而 cookie 是长期有效的，口令才是这次操作要证明的东西。
    """
    user_id = seed_admin(conn)
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)
    epoch_before = store.get_user(conn, user_id).credential_epoch  # type: ignore[union-attr]

    changed = service.change_password(
        conn,
        user_id=user_id,
        current_ticket=logged.ticket,
        old_password="wrong old password",
        new_password=NEW_PASSWORD,
        now=NOW,
    )

    assert changed is False
    assert store.get_user(conn, user_id).credential_epoch == epoch_before  # type: ignore[union-attr]
    assert isinstance(
        service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg),
        service.LoginSucceeded,
    )


def test_admin_reset_kills_every_session_including_the_asking_one(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """管理员重置与本人改密的**唯一差别**：一张票都不留。

    重置的前提通常是「这个人进不去了」或「凭据可能已泄露」。前者没有
    「当前会话」可保留，后者保留哪一条都是在赌。
    """
    user_id = seed_admin(conn)
    first = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    second = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(first, service.LoginSucceeded)
    assert isinstance(second, service.LoginSucceeded)

    service.reset_password(conn, user_id=user_id, new_password=NEW_PASSWORD, now=NOW)

    assert access(conn, first.ticket).outcome is Outcome.UNAUTHENTICATED
    assert access(conn, second.ticket).outcome is Outcome.UNAUTHENTICATED
    account = store.get_user(conn, user_id)
    assert account is not None and account.must_change_password is True
    assert isinstance(
        service.login(conn, email="admin@example.com", password=NEW_PASSWORD, now=NOW, config=cfg),
        service.LoginSucceeded,
    )


# ── 停用与恢复（I-V6 / I-V7） ──


def test_disable_kills_existing_sessions_immediately(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V6`：停用之后，旧 cookie 访问业务接口 401。

    这里断的是「立即」（不缓存），比判据的「一个配置窗口内」更强——原因是
    缓存本身还没建（`session_invalidate_seconds` 的消费者在格 6）。等缓存到位，
    这条仍然成立，只是窗口从 0 变成配置值。
    """
    user_id = seed_admin(conn)
    logged = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(logged, service.LoginSucceeded)

    assert service.disable_user(conn, user_id=user_id, now=NOW) is True

    decision = access(conn, logged.ticket)
    assert decision.outcome is Outcome.UNAUTHENTICATED
    # 原因码是 `session_expired` 而不是 `account_disabled`——停用在同一事务里
    # 吊销了票据，于是判定在 ① 步就停了，走不到 ② 步的 status。
    # 这不是遗漏：两者对外同为 401（I-V6 只要求这一点，§1.6 也要求文案相同），
    # 而 `account_disabled` 在**登录路径**上仍然可达（见下面那条断言），
    # 运维从记录里照样分得清「是管理员停的」还是「会话过期了」。
    assert decision.reason is ReasonCode.SESSION_EXPIRED
    row = store.get_ticket_by_hash(conn, credentials.hash_ticket(logged.ticket))
    assert row is not None and row.revoked_at is not None, (
        "只拦会话、不吊销行，旧 cookie 会活到过期"
    )

    blocked = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(blocked, service.LoginRejected)
    assert blocked.reason is ReasonCode.ACCOUNT_DISABLED


def test_disable_is_a_compare_and_set_not_a_blind_overwrite(
    conn: sqlite3.Connection,
) -> None:
    """两个管理员同时点「停用」，第二次必须**失败**而不是静默成功。

    盲目覆盖的版本里，两个人都看到成功——包括那个在别人恢复之后才点到的人，
    他会以为「停了」，而账户其实是活的。
    """
    user_id = seed_admin(conn)

    assert service.disable_user(conn, user_id=user_id, now=NOW) is True
    assert service.disable_user(conn, user_id=user_id, now=NOW) is False


def test_enable_restores_login_but_does_not_revive_old_tickets(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V6` 的后半句：恢复后能重新登录，拿到**新** cookie。

    旧票据不复活是刻意的——「停用前签发的」正是管理员想断掉的那批，
    而「停用期间签发的」根本不存在。
    """
    user_id = seed_admin(conn)
    old = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(old, service.LoginSucceeded)
    service.disable_user(conn, user_id=user_id, now=NOW)

    assert service.enable_user(conn, user_id=user_id, now=NOW) is True

    fresh = service.login(conn, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg)
    assert isinstance(fresh, service.LoginSucceeded)
    assert fresh.ticket != old.ticket
    assert access(conn, fresh.ticket).outcome is Outcome.ALLOW
    assert access(conn, old.ticket).outcome is Outcome.UNAUTHENTICATED


def test_disable_and_lockout_are_independent(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V7`：两列各自恢复、互不影响。

    合成一个 `enabled` 布尔之后，至少有一条断言会红，而用户看到的是
    「管理员恢复了我的账号，但我还是登不进去」或者反过来。
    """
    user_id = seed_admin(conn)

    service.disable_user(conn, user_id=user_id, now=NOW)
    disabled = store.get_user(conn, user_id)
    assert disabled is not None
    assert disabled.status is AccountStatus.DISABLED
    assert disabled.lockout_until is None, "停用不该顺手写锁定"

    service.enable_user(conn, user_id=user_id, now=NOW)
    for _ in range(cfg.login_max_failures):
        service.login(conn, email="admin@example.com", password="wrong", now=NOW, config=cfg)
    locked = store.get_user(conn, user_id)
    assert locked is not None
    assert locked.status is AccountStatus.ACTIVE, "锁定不该顺手改状态"
    assert locked.lockout_until is not None

    # 恢复状态（已经是 active，这一步应当失败）不影响锁定
    assert service.enable_user(conn, user_id=user_id, now=NOW) is False
    still_locked = store.get_user(conn, user_id)
    assert still_locked is not None and still_locked.lockout_until is not None


# ── 邀请（I-V13） ──


def test_invite_lands_in_the_inviters_team(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V13` 前半句：有效邀请建号，落在**邀请者的团队**。

    落到默认租户或新建团队都「能建号」，但那样的结果是新成员看不到任何资料，
    而他明明是被这个团队邀请来的。
    """
    admin_id = seed_admin(conn)
    tenant_id = tenant_of(conn, admin_id)
    _, token = service.create_invite(
        conn, tenant_id=tenant_id, role=Role.MEMBER, created_by=admin_id, now=NOW
    )

    accepted = service.accept_invite(
        conn, token=token, email="bob@example.com", password=PASSWORD, now=NOW, config=cfg
    )

    assert accepted.account.tenant_id == tenant_id
    assert accepted.account.role is Role.MEMBER
    assert access(conn, accepted.ticket).outcome is Outcome.ALLOW


def test_the_same_invite_cannot_be_used_twice(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """一次性是**原子守卫**给的，不是「先查再写」。

    先查再写在串行测试里永远绿；并发两次时两次都读到 `accepted_at is null`，
    于是同一张邀请建出两个账号，第二个人从此没有来源可追溯。
    """
    admin_id = seed_admin(conn)
    tenant_id = tenant_of(conn, admin_id)
    _, token = service.create_invite(
        conn, tenant_id=tenant_id, role=Role.MEMBER, created_by=admin_id, now=NOW
    )
    service.accept_invite(
        conn, token=token, email="bob@example.com", password=PASSWORD, now=NOW, config=cfg
    )

    with pytest.raises(service.InviteUnusable):
        service.accept_invite(
            conn, token=token, email="bob2@example.com", password=PASSWORD, now=NOW, config=cfg
        )

    assert store.get_user_by_email(conn, "bob2@example.com") is None, "拒绝之后不许留下半个账号"


def test_expired_invite_raises_the_same_thing_as_a_missing_one(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """`I-V13` 后半句：过期邀请与不存在的邀请，对内是**同一个异常**。

    响应逐字段相同由路由层保证，但那只在「两者走到同一个出口」时才可能成立。
    这里拆成两个异常类型的实现，路由层就必须自己记得把它们合成一个 404——
    而漏掉的那天不会有人发现，只会多一个能试探邀请是否存在的接口。
    """
    admin_id = seed_admin(conn)
    tenant_id = tenant_of(conn, admin_id)
    _, token = service.create_invite(
        conn,
        tenant_id=tenant_id,
        role=Role.MEMBER,
        created_by=admin_id,
        now=NOW,
        ttl_hours=1,
    )

    with pytest.raises(service.InviteUnusable) as expired:
        service.accept_invite(
            conn,
            token=token,
            email="bob@example.com",
            password=PASSWORD,
            now=NOW + timedelta(hours=2),
            config=cfg,
        )
    with pytest.raises(service.InviteUnusable) as missing:
        service.accept_invite(
            conn,
            token="never-issued",
            email="bob@example.com",
            password=PASSWORD,
            now=NOW,
            config=cfg,
        )

    assert type(expired.value) is type(missing.value)
    assert store.get_user_by_email(conn, "bob@example.com") is None, "过期邀请不许建出账号"


def test_an_invite_naming_an_email_only_works_for_that_email(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """定向邀请被转发给别人时不能用。

    不做这一条的话，「发给 A 的链接」在群里被转一次，进来的人就是 B——
    而邀请记录里写的是 A 的邮箱，事后追溯会指向一个没做错任何事的人。
    """
    admin_id = seed_admin(conn)
    tenant_id = tenant_of(conn, admin_id)
    _, token = service.create_invite(
        conn,
        tenant_id=tenant_id,
        role=Role.MEMBER,
        created_by=admin_id,
        now=NOW,
        email="bob@example.com",
    )

    with pytest.raises(service.InviteUnusable):
        service.accept_invite(
            conn, token=token, email="eve@example.com", password=PASSWORD, now=NOW, config=cfg
        )

    accepted = service.accept_invite(
        conn, token=token, email="BOB@example.com", password=PASSWORD, now=NOW, config=cfg
    )
    assert accepted.account.email == "bob@example.com"


def test_accepting_an_invite_for_a_taken_email_is_refused(
    conn: sqlite3.Connection, cfg: service.AuthConfig
) -> None:
    """邮箱全局唯一（`ux_users_email`）。冲突要**翻译**，不是把 sqlite 的报错漏出去。

    漏出去的表现是一次 500，而用户看到的是「系统坏了」，实际上是他已经在别的团队
    有了账号——那条信息对他有用，而且不泄露任何别人的数据。
    """
    admin_id = seed_admin(conn, email="admin@example.com")
    tenant_id = tenant_of(conn, admin_id)
    _, token = service.create_invite(
        conn, tenant_id=tenant_id, role=Role.MEMBER, created_by=admin_id, now=NOW
    )

    with pytest.raises(service.EmailAlreadyTaken):
        service.accept_invite(
            conn, token=token, email="admin@example.com", password=PASSWORD, now=NOW, config=cfg
        )


# ── 配置（I-V10 的可自动化部分） ──


def test_auth_config_reads_the_settings_it_is_given() -> None:
    """五个值必须**来自配置**。写死常量的实现，改配置之后行为不变。

    断言用非默认值：用默认值断言时，读配置与写常量两种实现的结果一样，
    测试区分不出它们——这正是 `I-V10` 的改坏法。
    """
    settings = Settings(
        auth=AuthSettings(
            session_invalidate_seconds=0,
            login_max_failures=7,
            lockout_seconds=120,
            ticket_ttl_hours=3,
            cookie_secure=True,
        )
    )

    config = service.AuthConfig.from_settings(settings)

    assert config.session_invalidate_seconds == 0
    assert config.login_max_failures == 7
    assert config.lockout_seconds == 120
    assert config.ticket_ttl_hours == 3
    assert config.cookie_secure is True


@pytest.mark.parametrize(
    "field",
    ["login_max_failures", "lockout_seconds", "ticket_ttl_hours"],
)
def test_zero_where_zero_means_silently_off_is_refused(field: str) -> None:
    """这几个键配 0 的后果是「功能悄悄消失」，不是报错。

    `login_max_failures=0` 是永不锁定，`ticket_ttl_hours=0` 是票据一签发就过期。
    两条都不会在启动时报错，只会在某天表现为「怎么没锁」或「怎么一直掉线」。

    **两层都拒**：配置层在启动时拒（进程起不来，问题当场暴露），
    服务层的 `AuthConfig` 在构造时也拒（绕过配置直接构造的路径同样守住）。
    只守一层的话，另一层就是那个「看起来有校验」的入口。
    """
    service_kwargs: dict[str, int] = {field: 0}
    with pytest.raises(ValueError, match="必须为正"):
        service.AuthConfig(**service_kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AuthSettings(**service_kwargs)  # type: ignore[arg-type]


def test_session_invalidate_seconds_may_be_zero() -> None:
    """0 是合法值：不缓存、吊销立即生效。测试拿确定性正是靠它（§1.5）。"""
    assert service.AuthConfig(session_invalidate_seconds=0).session_invalidate_seconds == 0
    assert (
        service.AuthConfig.from_settings(
            Settings(auth=AuthSettings(session_invalidate_seconds=0))
        ).session_invalidate_seconds
        == 0
    )
