mod activity;
mod classify;
mod proxy;

use clap::Parser;

/// GridFleet grid-relay fast-lane sidecar. CLI contract is frozen — the
/// agent (`agent_app/grid_node/sidecar.py::build_sidecar_command`) builds
/// these args; change both sides together.
#[derive(Parser, Debug)]
#[command(name = "gridfleet-relay-proxy", version)]
struct Args {
    /// host:port the sidecar listens on (the hub-advertised node address)
    #[arg(long)]
    listen: String,
    /// Appium upstream, e.g. http://127.0.0.1:4723
    #[arg(long)]
    appium: String,
    /// Python relay control upstream, e.g. http://127.0.0.1:7900
    #[arg(long)]
    control: String,
    /// Per-request upstream timeout in seconds
    #[arg(long, default_value_t = 60.0)]
    proxy_timeout: f64,
}

fn parse_upstream(value: &str) -> Result<(String, u16), String> {
    let rest = value
        .strip_prefix("http://")
        .ok_or_else(|| format!("upstream must be http://host:port, got {value}"))?;
    let rest = rest.trim_end_matches('/');
    let (host, port) = rest
        .rsplit_once(':')
        .ok_or_else(|| format!("missing port in upstream {value}"))?;
    if host.is_empty() {
        return Err(format!("missing host in upstream {value}"));
    }
    let port: u16 = port
        .parse()
        .map_err(|_| format!("invalid port in upstream {value}"))?;
    Ok((host.to_string(), port))
}

fn main() {
    let args = Args::parse();
    let appium = parse_upstream(&args.appium).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(2)
    });
    let control = parse_upstream(&args.control).unwrap_or_else(|e| {
        eprintln!("{e}");
        std::process::exit(2)
    });
    // rustls 0.23 requires an installed process-default crypto provider;
    // pingora's rustls feature does not install one.
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("install rustls crypto provider");

    let mut server = pingora::server::Server::new(None).unwrap();
    server.bootstrap();
    let relay = proxy::RelayProxy::new(
        appium,
        control,
        std::time::Duration::from_secs_f64(args.proxy_timeout),
    );
    let mut svc = pingora::proxy::http_proxy_service(&server.configuration, relay);
    svc.add_tcp(&args.listen);
    server.add_service(svc);
    server.run_forever();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_http_host_port() {
        assert_eq!(
            parse_upstream("http://127.0.0.1:4723"),
            Ok(("127.0.0.1".to_string(), 4723))
        );
        assert_eq!(
            parse_upstream("http://127.0.0.1:4723/"),
            Ok(("127.0.0.1".to_string(), 4723))
        );
    }

    #[test]
    fn rejects_bad_upstreams() {
        assert!(parse_upstream("https://127.0.0.1:1").is_err()); // sidecar speaks plain http
        assert!(parse_upstream("127.0.0.1:4723").is_err());
        assert!(parse_upstream("http://127.0.0.1").is_err());
        assert!(parse_upstream("http://:4723").is_err());
        assert!(parse_upstream("http://127.0.0.1:notaport").is_err());
    }
}
