use clap::Parser;

#[derive(Parser, Debug)]
#[command(name = "gridfleet-router")]
struct Args {
    /// host:port to listen on for WebDriver traffic, e.g. 0.0.0.0:4444
    #[arg(long)]
    listen: String,
    /// Backend base URL, e.g. http://backend:8000
    #[arg(long)]
    backend: String,
    /// Optional HTTP Basic machine credentials for backend calls: user:pass
    #[arg(long)]
    backend_auth: Option<String>,
    /// Per-command upstream timeout in seconds (covers slow Appium commands)
    #[arg(long, default_value_t = 300.0)]
    proxy_timeout: f64,
    /// Overall cap on a new-session request incl. queueing, seconds
    #[arg(long, default_value_t = 330.0)]
    new_session_timeout: f64,
}

fn main() {
    env_logger::init();
    let args = Args::parse();
    println!("{args:?}"); // replaced in Task 7
}
