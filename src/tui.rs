//! Terminal UI for live monitor updates.
//!
//! This module is intentionally presentation-focused:
//! - receives device updates from `monitor::MonitorRuntime`
//! - maintains selected-device state
//! - renders stream/device health and recent activity
//!
//! Analysis and synchronization policy lives outside the UI layer.

use std::collections::HashMap;
use std::io;
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Wrap};

use crate::config::{DeviceConfig, MonitorConfig};
use crate::monitor::{DeviceUpdate, MonitorRuntime};

struct DeviceView {
    cfg: DeviceConfig,
    last_update: Option<DeviceUpdate>,
}

struct App {
    devices: Vec<DeviceView>,
    index_by_ip: HashMap<String, usize>,
    selected: usize,
}

pub fn run_dashboard(config: MonitorConfig) -> Result<()> {
    let runtime = MonitorRuntime::start(&config);
    let run_result = run_terminal(config, &runtime);
    runtime.shutdown();
    run_result
}

fn run_terminal(config: MonitorConfig, runtime: &MonitorRuntime) -> Result<()> {
    let mut app = App::new(config);

    enable_raw_mode().context("failed to enable raw mode")?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen).context("failed to enter alt screen")?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend).context("failed to initialize terminal")?;

    let mut run_result = Ok(());

    while run_result.is_ok() {
        app.pull_updates(runtime);

        if let Err(err) = terminal.draw(|frame| app.draw(frame)) {
            run_result = Err(err).context("failed to draw frame");
            continue;
        }

        if event::poll(Duration::from_millis(100)).context("event poll failed")? {
            match event::read().context("event read failed")? {
                Event::Key(key) => {
                    if app.handle_key(key) {
                        break;
                    }
                }
                Event::Resize(_, _) => {}
                _ => {}
            }
        }
    }

    disable_raw_mode().ok();
    execute!(terminal.backend_mut(), LeaveAlternateScreen).ok();
    terminal.show_cursor().ok();

    run_result
}

impl App {
    fn new(config: MonitorConfig) -> Self {
        let mut devices = Vec::with_capacity(config.devices.len());
        let mut index_by_ip = HashMap::with_capacity(config.devices.len());

        for (idx, cfg) in config.devices.into_iter().enumerate() {
            index_by_ip.insert(cfg.bpm_ip.clone(), idx);
            devices.push(DeviceView {
                cfg,
                last_update: None,
            });
        }

        Self {
            devices,
            index_by_ip,
            selected: 0,
        }
    }

    fn pull_updates(&mut self, runtime: &MonitorRuntime) {
        while let Ok(update) = runtime.updates.try_recv() {
            if let Some(idx) = self.index_by_ip.get(&update.bpm_ip).copied() {
                if let Some(device) = self.devices.get_mut(idx) {
                    device.last_update = Some(update);
                }
            }
        }
    }

    fn handle_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Char('q') => true,
            KeyCode::Up | KeyCode::Char('k') => {
                if self.selected > 0 {
                    self.selected -= 1;
                }
                false
            }
            KeyCode::Down | KeyCode::Char('j') => {
                if self.selected + 1 < self.devices.len() {
                    self.selected += 1;
                }
                false
            }
            _ => false,
        }
    }

    fn draw(&self, frame: &mut ratatui::Frame<'_>) {
        let areas = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Min(1), Constraint::Length(2)])
            .split(frame.area());

        let body = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(45), Constraint::Percentage(55)])
            .split(areas[0]);

        self.draw_device_list(frame, body[0]);
        self.draw_details(frame, body[1]);

        let footer = Paragraph::new("q: quit   up/down or j/k: select device")
            .block(Block::default().borders(Borders::ALL).title("Controls"));
        frame.render_widget(footer, areas[1]);
    }

    fn draw_device_list(&self, frame: &mut ratatui::Frame<'_>, area: ratatui::layout::Rect) {
        let items = self
            .devices
            .iter()
            .map(|device| {
                let (status, trigger, arrivals, streams, active, age) =
                    match device.last_update.as_ref() {
                        Some(update) => {
                            let status = if update.last_error.is_some() {
                                if update.next_reconnect_ms.is_some() {
                                    "RETRY"
                                } else {
                                    "ERR"
                                }
                            } else if update.valid_streams == update.checked_streams
                                && update.checked_streams > 0
                            {
                                "LIVE"
                            } else {
                                "OK"
                            };
                            (
                                status,
                                update
                                    .last_event_id
                                    .as_deref()
                                    .map(ellipsize)
                                    .unwrap_or_else(|| "-".to_string()),
                                update.arrival_count,
                                format!("{}/{}", update.valid_streams, update.checked_streams),
                                update.active_streams,
                                age_string(update.observed_at),
                            )
                        }
                        None => (
                            "WAIT",
                            "-".to_string(),
                            0,
                            format!("0/{}", device.cfg.stream_keys.len()),
                            0,
                            "-".to_string(),
                        ),
                    };
                let redis_addr = device
                    .last_update
                    .as_ref()
                    .map(|u| u.redis_addr.clone())
                    .unwrap_or_else(|| device.cfg.redis.display_addr());

                ListItem::new(vec![
                    Line::from(format!("{} [{}] {}", device.cfg.bpm_ip, status, redis_addr)),
                    Line::from(format!(
                        "arrivals={} streams={} active={} age={} last_id={}",
                        arrivals, streams, active, age, trigger
                    )),
                ])
            })
            .collect::<Vec<_>>();

        let mut state = ListState::default();
        if !self.devices.is_empty() {
            state.select(Some(self.selected.min(self.devices.len() - 1)));
        }

        let list = List::new(items)
            .block(Block::default().title("Digitizers").borders(Borders::ALL))
            .highlight_style(
                Style::default()
                    .fg(Color::Yellow)
                    .add_modifier(Modifier::BOLD),
            )
            .highlight_symbol("-> ");

        frame.render_stateful_widget(list, area, &mut state);
    }

    fn draw_details(&self, frame: &mut ratatui::Frame<'_>, area: ratatui::layout::Rect) {
        let Some(device) = self.devices.get(self.selected) else {
            let placeholder = Paragraph::new("No devices in config")
                .block(Block::default().borders(Borders::ALL).title("Details"));
            frame.render_widget(placeholder, area);
            return;
        };

        let mut lines = Vec::new();
        lines.push(format!(
            "label: {}",
            device
                .last_update
                .as_ref()
                .map(|u| u.device_label.as_str())
                .unwrap_or(&device.cfg.label)
        ));
        lines.push(format!("bpm_ip: {}", device.cfg.bpm_ip));
        lines.push(format!("redis: {}", device.cfg.redis.to_url()));
        lines.push(format!(
            "configured keys: trigger={} fallback={} tbt={}",
            1,
            device.cfg.trigger_fallback_keys.len(),
            device.cfg.stream_keys.len()
        ));

        match device.last_update.as_ref() {
            Some(update) => {
                if let Some(err) = update.last_error.as_ref() {
                    lines.push(format!("last error: {}", err));
                }
                if let Some(delay) = update.next_reconnect_ms {
                    lines.push(format!("next reconnect in: {} ms", delay));
                }

                lines.push(format!(
                    "streams valid/checked/active: {}/{}/{}",
                    update.valid_streams, update.checked_streams, update.active_streams
                ));
                lines.push(format!("arrivals seen: {}", update.arrival_count));
                lines.push(format!(
                    "latest id: {}",
                    update
                        .last_event_id
                        .as_deref()
                        .map(ellipsize)
                        .unwrap_or_else(|| "-".to_string())
                ));
                lines.push("recent ids (latest 5):".to_string());
                if update.recent_event_ids.is_empty() {
                    lines.push("  -".to_string());
                } else {
                    for id in update.recent_event_ids.iter().rev() {
                        lines.push(format!("  {}", id));
                    }
                }

                lines.push("stream snapshots:".to_string());
                for stream in &update.stream_states {
                    let last_id = stream
                        .last_entry_id
                        .as_deref()
                        .map(ellipsize)
                        .unwrap_or_else(|| "-".to_string());
                    let bytes = stream
                        .payload_bytes
                        .map(|v| v.to_string())
                        .unwrap_or_else(|| "-".to_string());
                    let key = ellipsize(&stream.key);
                    lines.push(format!(
                        "  {} | type={} events={} last_id={} _bytes={} has_underscore={}",
                        key,
                        stream.value_type,
                        stream.entries_seen,
                        last_id,
                        bytes,
                        stream.has_payload_field
                    ));
                }
            }
            None => {
                lines.push("waiting for first poll result...".to_string());
                lines.push("configured keys:".to_string());
                lines.push(format!("  {}", ellipsize(&device.cfg.trigger_key)));
                for key in &device.cfg.trigger_fallback_keys {
                    lines.push(format!("  {}", ellipsize(key)));
                }
                for key in &device.cfg.stream_keys {
                    lines.push(format!("  {}", ellipsize(key)));
                }
            }
        }

        let paragraph = Paragraph::new(lines.join("\n"))
            .block(
                Block::default()
                    .title("Selected Device")
                    .borders(Borders::ALL),
            )
            .wrap(Wrap { trim: false });

        frame.render_widget(paragraph, area);
    }
}

fn age_string(time: SystemTime) -> String {
    match SystemTime::now().duration_since(time) {
        Ok(elapsed) => {
            let ms = elapsed.as_millis();
            if ms < 1_000 {
                format!("{}ms", ms)
            } else {
                format!("{:.1}s", (ms as f64) / 1_000.0)
            }
        }
        Err(_) => "0ms".to_string(),
    }
}

fn ellipsize(input: &str) -> String {
    const LIMIT: usize = 72;
    if input.len() <= LIMIT {
        return input.to_string();
    }

    let keep = LIMIT / 2;
    format!("{}...{}", &input[..keep], &input[input.len() - keep..])
}
