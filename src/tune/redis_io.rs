//! Redis wire format shared by the processor and the PVXS bridge.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail};
use redis::Connection;

pub const PREFIX: &str = "{DR:TUNE}:";
pub type StreamEntry = (String, Vec<(Vec<u8>, Vec<u8>)>);
pub type StreamEntries = Vec<StreamEntry>;

const OUTPUT_KEYS: [&str; 12] = [
    "HEARTBEAT_R",
    "SOURCE_AGE_MS_R",
    "STATE_R",
    "RESULT_R",
    "SPEC_R",
    "SPEC_STATUS_R",
    "SCHEMA_R",
    "DEFAULT_SPEC_R",
    "H:Q_R",
    "V:Q_R",
    "H:STATUS_R",
    "V:STATUS_R",
];

// MULTI alone permits earlier commands to commit when a later XADD fails.
// Preflight every key and timestamp before any writes, in one Redis execution.
const PUBLISH_SCRIPT: &str = r#"
local function uint_less(a,b)
    a = string.gsub(a, '^0+', ''); b = string.gsub(b, '^0+', '')
    if #a ~= #b then return #a < #b end
    return a < b
end
local function id_less(a,b)
    local am,as = string.match(a, '^(%d+)%-(%d+)$')
    local bm,bs = string.match(b, '^(%d+)%-(%d+)$')
    if not am or not bm then return false end
    if am == bm then return uint_less(as,bs) end
    return uint_less(am,bm)
end
for _,key in ipairs(KEYS) do
    local kind = redis.call('TYPE',key).ok
    if kind ~= 'none' and kind ~= 'stream' then
        return redis.error_reply('publication target is not a stream: '..key)
    end
    if kind == 'stream' then
        local last = redis.call('XREVRANGE',key,'+','-','COUNT',1)
        if #last > 0 and not id_less(last[1][1],ARGV[1]) then
            return redis.error_reply('publication timestamp is not newer: '..key)
        end
    end
end
for i,key in ipairs(KEYS) do
    redis.call('XADD',key,'MAXLEN',ARGV[2*i+1],ARGV[1],'_',ARGV[2*i])
end
return 1
"#;

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(u64::MAX as u128) as u64
}

pub fn stream_id(id: &str) -> Result<(u64, u64)> {
    let (ms, ns) = id
        .split_once('-')
        .ok_or_else(|| anyhow!("invalid stream ID {id}"))?;
    Ok((
        ms.parse().context("invalid stream ID milliseconds")?,
        ns.parse().context("invalid stream ID remainder")?,
    ))
}

pub fn payload(fields: &[(Vec<u8>, Vec<u8>)]) -> Result<&[u8]> {
    let mut matches = fields.iter().filter(|(key, _)| key == b"_");
    let first = matches
        .next()
        .ok_or_else(|| anyhow!("stream entry has no '_' payload"))?;
    if matches.next().is_some() {
        bail!("stream entry has duplicate '_' payloads");
    }
    Ok(&first.1)
}

pub fn connect(url: &str, timeout: Duration) -> Result<Connection> {
    let client = redis::Client::open(url).context("invalid Redis endpoint")?;
    let conn = client
        .get_connection_with_timeout(timeout)
        .context("Redis connection failed")?;
    conn.set_read_timeout(Some(timeout))?;
    conn.set_write_timeout(Some(timeout))?;
    Ok(conn)
}

pub fn latest(conn: &mut Connection, key: &str) -> Result<Option<StreamEntry>> {
    let mut entries: StreamEntries = redis::cmd("XREVRANGE")
        .arg(key)
        .arg("+")
        .arg("-")
        .arg("COUNT")
        .arg(1)
        .query(conn)?;
    Ok(entries.pop())
}

/// The bridge interprets stream sequence as a nanosecond remainder. Incrementing
/// a Redis auto-ID would silently manufacture a timestamp, so use the wall clock.
/// Backwards clocks are rejected until time catches up with the last publication.
pub struct Publisher {
    conn: Connection,
    last_id: (u64, u64),
}

impl Publisher {
    pub fn connect(url: &str) -> Result<Self> {
        let mut conn = connect(url, Duration::from_millis(500))?;
        let mut last_id = (0, 0);
        for suffix in OUTPUT_KEYS {
            if let Some((id, _)) = latest(&mut conn, &format!("{PREFIX}{suffix}"))? {
                last_id = last_id.max(stream_id(&id)?);
            }
        }
        Ok(Self { conn, last_id })
    }

    pub fn connection(&mut self) -> &mut Connection {
        &mut self.conn
    }

    /// Preflight and write in one Redis script. RESULT_R is self-contained.
    pub fn publish(&mut self, values: &[(&str, Vec<u8>)]) -> Result<()> {
        let since_epoch = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .context("publication clock precedes Unix epoch")?;
        let stamp = (
            since_epoch.as_secs().saturating_mul(1000) + u64::from(since_epoch.subsec_millis()),
            u64::from(since_epoch.subsec_nanos() % 1_000_000),
        );
        if stamp <= self.last_id {
            bail!("publication clock is not later than the last committed timestamp");
        }
        let id = format!("{}-{}", stamp.0, stamp.1);
        let mut command = redis::cmd("EVAL");
        command.arg(PUBLISH_SCRIPT).arg(values.len());
        for (suffix, _) in values {
            command.arg(format!("{PREFIX}{suffix}"));
        }
        command.arg(&id);
        for (suffix, bytes) in values {
            command
                .arg(bytes)
                .arg(if *suffix == "RESULT_R" { 4 } else { 16 });
        }
        command
            .query::<i64>(&mut self.conn)
            .context("derived Redis publication failed")?;
        self.last_id = stamp;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_only_the_exact_payload_and_id() {
        assert_eq!(
            stream_id("1700000000000-123456").unwrap(),
            (1700000000000, 123456)
        );
        assert!(stream_id("1700000000000").is_err());
        assert!(payload(&[(b"other".to_vec(), vec![1])]).is_err());
        assert!(payload(&[(b"_".to_vec(), vec![1]), (b"_".to_vec(), vec![2])]).is_err());
    }
}
