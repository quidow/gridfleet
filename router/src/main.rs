use std::sync::Arc;
use std::time::Duration;

use clap::Parser;

use gridfleet_router::activity::ActivityTracker;
use gridfleet_router::backend::BackendClient;
use gridfleet_router::proxy::GridRouter;
use gridfleet_router::routes::RouteMap;
use gridfleet_router::tasks::{spawn_activity_flush, spawn_route_reconcile};

#[derive(Parser, Debug)]
#[command(name = "gridfleet-router")]
struct Args {
    /// host:port to listen on for WebDriver traffic, e.g. 0.0.0.0:4444
    #[arg(long, env = "GRIDFLEET_ROUTER_LISTEN")]
    listen: String,
    /// Backend base URL, e.g. http://backend:8000
    #[arg(long, env = "GRIDFLEET_ROUTER_BACKEND")]
    backend: String,
    /// Optional HTTP Basic machine credentials for backend calls: user:pass
    #[arg(long, env = "GRIDFLEET_ROUTER_BACKEND_AUTH")]
    backend_auth: Option<String>,
    /// Per-command upstream timeout in seconds (covers slow Appium commands)
    #[arg(long, default_value_t = 300.0, env = "GRIDFLEET_ROUTER_PROXY_TIMEOUT")]
    proxy_timeout: f64,
    /// Overall cap on a new-session request incl. queueing, seconds
    #[arg(
        long,
        default_value_t = 330.0,
        env = "GRIDFLEET_ROUTER_NEW_SESSION_TIMEOUT"
    )]
    new_session_timeout: f64,
}

/// Parse `user:pass` into a credential tuple. A missing colon is a fatal
/// config error (the CLI contract is `user:pass`).
fn parse_auth(value: &str) -> Result<(String, String), String> {
    value
        .split_once(':')
        .map(|(u, p)| (u.to_string(), p.to_string()))
        .ok_or_else(|| format!("--backend-auth must be user:pass, got {value}"))
}

fn main() {
    env_logger::init();
    let args = Args::parse();

    let auth = match args.backend_auth.as_deref().map(parse_auth) {
        Some(Ok(a)) => Some(a),
        Some(Err(e)) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
        None => None,
    };

    // rustls 0.23 requires an installed process-default crypto provider;
    // pingora's rustls feature does not install one.
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("install rustls crypto provider");

    let routes = Arc::new(RouteMap::default());
    let activity = Arc::new(ActivityTracker::default());
    let backend = Arc::new(BackendClient::new(&args.backend, auth));

    // Periodic maintenance loops need a tokio runtime. pingora owns its own
    // worker runtimes for request handling but does not expose a handle for
    // auxiliary work, so we host the loops on a dedicated thread+runtime that
    // lives for the process lifetime (run_forever below never returns).
    {
        let routes = routes.clone();
        let activity = activity.clone();
        let backend = backend.clone();
        std::thread::Builder::new()
            .name("router-maintenance".into())
            .spawn(move || {
                let rt = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("maintenance runtime");
                rt.block_on(async move {
                    spawn_route_reconcile(routes, backend.clone());
                    spawn_activity_flush(activity, backend);
                    std::future::pending::<()>().await;
                });
            })
            .expect("spawn maintenance thread");
    }

    let mut server = pingora::server::Server::new(None).unwrap();
    server.bootstrap();
    let router = GridRouter {
        routes,
        activity,
        backend,
        proxy_timeout: Duration::from_secs_f64(args.proxy_timeout),
        new_session_timeout: Duration::from_secs_f64(args.new_session_timeout),
    };
    let mut svc = pingora::proxy::http_proxy_service(&server.configuration, router);
    svc.add_tcp(&args.listen);
    server.add_service(svc);
    server.run_forever();
}

#[cfg(test)]
mod tests {
    use super::{parse_auth, Args};
    use clap::Parser;

    #[test]
    fn parses_user_pass() {
        assert_eq!(
            parse_auth("alice:s3cr3t"),
            Ok(("alice".to_string(), "s3cr3t".to_string()))
        );
        // password may contain colons
        assert_eq!(
            parse_auth("u:p:with:colons"),
            Ok(("u".to_string(), "p:with:colons".to_string()))
        );
    }

    #[test]
    fn rejects_missing_colon() {
        assert!(parse_auth("nopass").is_err());
    }

    /// Env vars supply flag values when the corresponding flag is absent from
    /// argv. Run serially (single thread) to avoid racing other env mutators.
    #[test]
    fn env_vars_are_honored() {
        let vars = [
            ("GRIDFLEET_ROUTER_LISTEN", "0.0.0.0:4444"),
            ("GRIDFLEET_ROUTER_BACKEND", "http://backend:8000"),
            ("GRIDFLEET_ROUTER_BACKEND_AUTH", "alice:s3cr3t"),
            ("GRIDFLEET_ROUTER_PROXY_TIMEOUT", "120.5"),
            ("GRIDFLEET_ROUTER_NEW_SESSION_TIMEOUT", "200"),
        ];
        for (k, v) in vars {
            // SAFETY: tests in this module run single-threaded relative to
            // these unique GRIDFLEET_ROUTER_* vars (no other test touches them).
            unsafe { std::env::set_var(k, v) };
        }

        let args = Args::try_parse_from(["gridfleet-router"]).expect("parse from env");

        assert_eq!(args.listen, "0.0.0.0:4444");
        assert_eq!(args.backend, "http://backend:8000");
        assert_eq!(args.backend_auth.as_deref(), Some("alice:s3cr3t"));
        assert_eq!(args.proxy_timeout, 120.5);
        assert_eq!(args.new_session_timeout, 200.0);

        for (k, _) in vars {
            // SAFETY: same as above.
            unsafe { std::env::remove_var(k) };
        }
    }
}
