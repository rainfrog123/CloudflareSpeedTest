#!/usr/bin/env python3
"""
CloudflareSpeedTest - Python Version
A tool to test latency and download speed to Cloudflare IPs and find the best ones.
"""

import argparse
import asyncio
import csv
import ipaddress
import json
import random
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp

VERSION = "1.0.0"

# Default values matching Go version
DEFAULT_ROUTINES = 200
DEFAULT_PING_TIMES = 4
DEFAULT_TEST_COUNT = 10
DEFAULT_DOWNLOAD_TIME = 10
DEFAULT_TCP_PORT = 443
DEFAULT_URL = "https://cf.xiu2.xyz/url"
DEFAULT_MAX_DELAY = 9999
DEFAULT_MIN_DELAY = 0
DEFAULT_MAX_LOSS_RATE = 1.0
DEFAULT_MIN_SPEED = 0.0
DEFAULT_PRINT_NUM = 10
DEFAULT_IP_FILE = "ip.txt"
DEFAULT_OUTPUT_FILE = "result.csv"


@dataclass
class PingResult:
    """Result of ping test for a single IP."""
    ip: str
    sent: int = 0
    received: int = 0
    total_delay: float = 0.0
    colo: str = ""

    @property
    def loss_rate(self) -> float:
        if self.sent == 0:
            return 1.0
        return (self.sent - self.received) / self.sent

    @property
    def avg_delay(self) -> float:
        if self.received == 0:
            return 0.0
        return self.total_delay / self.received


@dataclass
class SpeedResult:
    """Result of speed test for a single IP."""
    ip: str
    sent: int = 0
    received: int = 0
    total_delay: float = 0.0
    download_speed: float = 0.0  # bytes/s
    colo: str = ""

    @property
    def loss_rate(self) -> float:
        if self.sent == 0:
            return 1.0
        return (self.sent - self.received) / self.sent

    @property
    def avg_delay(self) -> float:
        if self.received == 0:
            return 0.0
        return self.total_delay / self.received

    @property
    def speed_mbps(self) -> float:
        return self.download_speed / (1024 * 1024)


class EWMA:
    """Exponentially Weighted Moving Average."""
    def __init__(self, age: float = 30.0):
        self.alpha = 2.0 / (age + 1.0)
        self.value = 0.0
        self.initialized = False

    def add(self, value: float):
        if not self.initialized:
            self.value = value
            self.initialized = True
        else:
            self.value = self.alpha * value + (1 - self.alpha) * self.value


class ProgressBar:
    """Simple progress bar for terminal output."""
    def __init__(self, total: int, prefix: str = ""):
        self.total = total
        self.current = 0
        self.prefix = prefix
        self.start_time = time.time()

    def update(self, n: int = 1):
        self.current += n
        self._display()

    def _display(self):
        if self.total == 0:
            return
        percent = self.current / self.total * 100
        filled = int(50 * self.current / self.total)
        bar = "█" * filled + "░" * (50 - filled)
        elapsed = time.time() - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0
        sys.stdout.write(f"\r{self.prefix} |{bar}| {percent:.1f}% ({self.current}/{self.total}) [{rate:.1f}/s]")
        sys.stdout.flush()
        if self.current >= self.total:
            sys.stdout.write("\n")

    def finish(self):
        self.current = self.total
        self._display()


def load_ip_ranges(ip_file: str, ip_text: str) -> list[str]:
    """Load IP ranges from file or command line."""
    cidrs = []
    
    if ip_text:
        cidrs = [s.strip() for s in ip_text.split(",") if s.strip()]
    else:
        try:
            with open(ip_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        cidrs.append(line)
        except FileNotFoundError:
            print(f"Error: IP file '{ip_file}' not found")
            sys.exit(1)
    
    return cidrs


def generate_ips(cidrs: list[str], all_ip: bool = False) -> list[str]:
    """Generate IP addresses from CIDR ranges."""
    ips = []
    
    for cidr in cidrs:
        try:
            if "/" not in cidr:
                # Single IP
                ips.append(cidr)
                continue
            
            network = ipaddress.ip_network(cidr, strict=False)
            
            if isinstance(network, ipaddress.IPv4Network):
                if all_ip:
                    # All IPs in range
                    for ip in network.hosts():
                        ips.append(str(ip))
                else:
                    # Random IP per /24 subnet (or smaller)
                    prefix_len = network.prefixlen
                    if prefix_len >= 24:
                        # Small subnet, pick random host
                        hosts = list(network.hosts())
                        if hosts:
                            ips.append(str(random.choice(hosts)))
                    else:
                        # Large subnet, enumerate /24s and pick one random from each
                        for subnet in network.subnets(new_prefix=24):
                            hosts = list(subnet.hosts())
                            if hosts:
                                ips.append(str(random.choice(hosts)))
            else:
                # IPv6: random sampling
                if network.prefixlen == 128:
                    ips.append(str(network.network_address))
                else:
                    # Generate some random IPs from the range
                    num_samples = min(100, 2 ** (128 - network.prefixlen))
                    network_int = int(network.network_address)
                    host_bits = 128 - network.prefixlen
                    for _ in range(num_samples):
                        random_host = random.randint(0, (2 ** host_bits) - 1)
                        ip_int = network_int + random_host
                        ips.append(str(ipaddress.IPv6Address(ip_int)))
        except ValueError as e:
            print(f"Warning: Invalid CIDR '{cidr}': {e}")
    
    return ips


async def tcp_ping(ip: str, port: int, timeout: float = 1.0) -> Optional[float]:
    """Perform TCP ping to IP:port, return latency in ms or None on failure."""
    try:
        start = time.time()
        if ":" in ip:
            # IPv6
            addr = (ip, port, 0, 0)
            family = socket.AF_INET6
        else:
            addr = (ip, port)
            family = socket.AF_INET
        
        loop = asyncio.get_event_loop()
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        
        try:
            await asyncio.wait_for(
                loop.sock_connect(sock, addr),
                timeout=timeout
            )
            latency = (time.time() - start) * 1000  # ms
            return latency
        finally:
            sock.close()
    except Exception:
        return None


def create_forced_connector(ip: str, port: int, ssl_context):
    """Create a connector that forces connections to a specific IP."""
    class ForcedIPConnector(aiohttp.TCPConnector):
        async def _resolve_host(self, host: str, port_: int, traces=None):
            return [
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
            ]
    return ForcedIPConnector(ssl=ssl_context)


async def http_ping(
    ip: str,
    port: int,
    url: str,
    timeout: float = 2.0,
    valid_codes: set[int] = None
) -> tuple[Optional[float], str]:
    """
    Perform HTTP HEAD ping, return (latency_ms, colo) or (None, "").
    Forces connection to specific IP while using URL's host for SNI/Host header.
    """
    if valid_codes is None:
        valid_codes = {200, 301, 302}
    
    parsed = urlparse(url)
    scheme = parsed.scheme
    
    # Create SSL context for HTTPS
    ssl_context = None
    if scheme == "https":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    connector = create_forced_connector(ip, port, ssl_context)
    
    try:
        start = time.time()
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.head(
                url,
                allow_redirects=False,
                headers={"User-Agent": "CloudflareSpeedTest/Python"}
            ) as response:
                latency = (time.time() - start) * 1000
                
                if response.status not in valid_codes:
                    return None, ""
                
                # Extract colo from headers
                colo = ""
                # Cloudflare
                cf_ray = response.headers.get("cf-ray", "")
                if cf_ray and "-" in cf_ray:
                    colo = cf_ray.split("-")[-1]
                # AWS CloudFront
                if not colo:
                    colo = response.headers.get("x-amz-cf-pop", "")
                # Fastly
                if not colo:
                    served_by = response.headers.get("x-served-by", "")
                    if served_by:
                        parts = served_by.split("-")
                        if len(parts) >= 3:
                            colo = parts[-2]
                
                return latency, colo
    except Exception:
        return None, ""
    finally:
        await connector.close()


async def test_latency(
    ips: list[str],
    port: int,
    ping_times: int,
    routines: int,
    use_httping: bool,
    url: str,
    httping_code: int,
    colo_filter: set[str],
    debug: bool
) -> list[PingResult]:
    """Test latency for all IPs with concurrency limit."""
    results = []
    semaphore = asyncio.Semaphore(routines)
    progress = ProgressBar(len(ips), "Testing latency")
    
    valid_codes = {200, 301, 302} if httping_code == 0 else {httping_code}
    
    async def test_ip(ip: str) -> Optional[PingResult]:
        async with semaphore:
            result = PingResult(ip=ip)
            colo = ""
            
            for i in range(ping_times):
                result.sent += 1
                
                if use_httping:
                    latency, colo = await http_ping(ip, port, url, timeout=2.0, valid_codes=valid_codes)
                    if latency is not None:
                        # Check colo filter on first successful ping
                        if i == 0 and colo_filter and colo not in colo_filter:
                            progress.update()
                            return None
                        result.received += 1
                        result.total_delay += latency
                        result.colo = colo
                else:
                    latency = await tcp_ping(ip, port, timeout=1.0)
                    if latency is not None:
                        result.received += 1
                        result.total_delay += latency
            
            progress.update()
            
            if result.received == 0:
                return None
            return result
    
    tasks = [test_ip(ip) for ip in ips]
    results_raw = await asyncio.gather(*tasks)
    results = [r for r in results_raw if r is not None]
    
    progress.finish()
    return results


def filter_by_delay(results: list[PingResult], min_delay: float, max_delay: float) -> list[PingResult]:
    """Filter results by delay range."""
    if min_delay == DEFAULT_MIN_DELAY and max_delay == DEFAULT_MAX_DELAY:
        return results
    return [r for r in results if min_delay <= r.avg_delay <= max_delay]


def filter_by_loss_rate(results: list[PingResult], max_loss: float) -> list[PingResult]:
    """Filter results by maximum loss rate."""
    if max_loss >= 1.0:
        return results
    return [r for r in results if r.loss_rate <= max_loss]


def sort_ping_results(results: list[PingResult]) -> list[PingResult]:
    """Sort by loss rate (ascending), then by delay (ascending)."""
    return sorted(results, key=lambda r: (r.loss_rate, r.avg_delay))


async def test_download_speed(
    ping_results: list[PingResult],
    url: str,
    port: int,
    download_time: float,
    test_count: int,
    min_speed: float,
    debug: bool
) -> list[SpeedResult]:
    """Test download speed for top ping results."""
    results = []
    
    # Determine how many to test
    if min_speed > 0:
        num_to_test = len(ping_results)
    else:
        num_to_test = min(test_count, len(ping_results))
    
    if num_to_test == 0:
        return results
    
    progress = ProgressBar(num_to_test, "Testing speed ")
    
    parsed = urlparse(url)
    scheme = parsed.scheme
    
    ssl_context = None
    if scheme == "https":
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    
    for ping_result in ping_results[:num_to_test]:
        ip = ping_result.ip
        connector = create_forced_connector(ip, port, ssl_context)
        
        speed_result = SpeedResult(
            ip=ip,
            sent=ping_result.sent,
            received=ping_result.received,
            total_delay=ping_result.total_delay,
            colo=ping_result.colo
        )
        
        try:
            timeout = aiohttp.ClientTimeout(total=download_time + 5)
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            ) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "CloudflareSpeedTest/Python"}
                ) as response:
                    if response.status != 200:
                        if debug:
                            print(f"\nDownload HTTP {response.status} for {ip}")
                        progress.update()
                        continue
                    
                    # Extract colo if not already set
                    if not speed_result.colo:
                        cf_ray = response.headers.get("cf-ray", "")
                        if cf_ray and "-" in cf_ray:
                            speed_result.colo = cf_ray.split("-")[-1]
                    
                    # Download and measure speed with EWMA
                    ewma = EWMA(age=30.0)
                    start_time = time.time()
                    interval = download_time / 100.0
                    last_check = start_time
                    bytes_since_check = 0
                    total_bytes = 0
                    
                    async for chunk in response.content.iter_chunked(1024):
                        total_bytes += len(chunk)
                        bytes_since_check += len(chunk)
                        
                        now = time.time()
                        elapsed_total = now - start_time
                        elapsed_check = now - last_check
                        
                        if elapsed_total >= download_time:
                            # Final update
                            if elapsed_check > 0:
                                speed = bytes_since_check / elapsed_check
                                ewma.add(speed)
                            break
                        
                        if elapsed_check >= interval:
                            speed = bytes_since_check / elapsed_check
                            ewma.add(speed)
                            bytes_since_check = 0
                            last_check = now
                    
                    # Calculate final speed
                    elapsed = time.time() - start_time
                    if elapsed > 0 and total_bytes > 0:
                        # Simple bytes/sec calculation
                        speed_result.download_speed = total_bytes / elapsed
                    
                    if debug:
                        print(f"\nDownload {ip}: {total_bytes} bytes in {elapsed:.2f}s, speed={speed_result.speed_mbps:.2f} MB/s")
        except Exception as e:
            if debug:
                print(f"\nDownload error for {ip}: {e}")
        finally:
            await connector.close()
        
        progress.update()
        
        # Check minimum speed
        if min_speed > 0:
            if speed_result.speed_mbps >= min_speed:
                results.append(speed_result)
                if len(results) >= test_count:
                    break
        else:
            results.append(speed_result)
    
    progress.finish()
    
    # Sort by download speed descending
    results.sort(key=lambda r: r.download_speed, reverse=True)
    
    return results


def print_results(results: list[SpeedResult], print_num: int, has_download: bool):
    """Print results to console."""
    if print_num == 0 or not results:
        return
    
    results_to_print = results[:print_num]
    
    # Check if we have IPv6 (need wider column)
    max_ip_len = max(len(r.ip) for r in results_to_print)
    ip_width = max(15, max_ip_len)
    
    # Header
    header_format = f"{{:<{ip_width}}} {{:>6}} {{:>8}} {{:>10}} {{:>12}} {{:>14}} {{:>8}}"
    row_format = f"{{:<{ip_width}}} {{:>6}} {{:>8}} {{:>10.2%}} {{:>12.2f}} {{:>14.2f}} {{:>8}}"
    
    print()
    print(header_format.format("IP Address", "Sent", "Received", "Loss Rate", "Avg Delay", "Speed(MB/s)", "Colo"))
    print("-" * (ip_width + 70))
    
    for r in results_to_print:
        speed = r.speed_mbps if has_download else 0.0
        colo = r.colo if r.colo else "N/A"
        print(row_format.format(
            r.ip,
            r.sent,
            r.received,
            r.loss_rate,
            r.avg_delay,
            speed,
            colo
        ))
    
    print()


def export_csv(results: list[SpeedResult], output_file: str, has_download: bool):
    """Export results to CSV file."""
    if not output_file or output_file.strip() == "" or not results:
        return
    
    try:
        with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            # Chinese headers to match Go version
            writer.writerow([
                "IP 地址", "已发送", "已接收", "丢包率", 
                "平均延迟", "下载速度(MB/s)", "地区码"
            ])
            
            for r in results:
                speed = f"{r.speed_mbps:.2f}" if has_download else "0.00"
                colo = r.colo if r.colo else ""
                writer.writerow([
                    r.ip,
                    r.sent,
                    r.received,
                    f"{r.loss_rate:.2%}",
                    f"{r.avg_delay:.2f}",
                    speed,
                    colo
                ])
        
        print(f"Results saved to: {output_file}")
    except Exception as e:
        print(f"Error saving CSV: {e}")


def export_json(results: list[SpeedResult], output_file: str, has_download: bool):
    """Export results to JSON file."""
    if not output_file or output_file.strip() == "" or not results:
        return
    
    try:
        data = []
        for r in results:
            data.append({
                "ip": r.ip,
                "sent": r.sent,
                "received": r.received,
                "loss_rate": round(r.loss_rate, 4),
                "avg_delay_ms": round(r.avg_delay, 2),
                "download_speed_mbps": round(r.speed_mbps, 2) if has_download else 0.0,
                "colo": r.colo if r.colo else None
            })
        
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"Results saved to: {output_file}")
    except Exception as e:
        print(f"Error saving JSON: {e}")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="CloudflareSpeedTest - Test latency and download speed to Cloudflare IPs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument("-n", type=int, default=DEFAULT_ROUTINES,
                        help="Number of concurrent latency test routines (max 1000 recommended)")
    parser.add_argument("-t", type=int, default=DEFAULT_PING_TIMES,
                        help="Number of latency tests per IP")
    parser.add_argument("-dn", type=int, default=DEFAULT_TEST_COUNT,
                        help="Number of IPs to test for download speed")
    parser.add_argument("-dt", type=float, default=DEFAULT_DOWNLOAD_TIME,
                        help="Download test duration in seconds")
    parser.add_argument("-tp", type=int, default=DEFAULT_TCP_PORT,
                        help="TCP port for testing")
    parser.add_argument("-url", type=str, default=DEFAULT_URL,
                        help="URL for HTTPing and download test")
    parser.add_argument("-httping", action="store_true",
                        help="Use HTTP HEAD for latency test instead of TCP")
    parser.add_argument("-httping-code", type=int, default=0,
                        help="Valid HTTP status code (0 = accept 200/301/302)")
    parser.add_argument("-cfcolo", type=str, default="",
                        help="Filter by Cloudflare colo codes (comma-separated)")
    parser.add_argument("-tl", type=float, default=DEFAULT_MAX_DELAY,
                        help="Maximum average latency in ms")
    parser.add_argument("-tll", type=float, default=DEFAULT_MIN_DELAY,
                        help="Minimum average latency in ms")
    parser.add_argument("-tlr", type=float, default=DEFAULT_MAX_LOSS_RATE,
                        help="Maximum packet loss rate (0-1)")
    parser.add_argument("-sl", type=float, default=DEFAULT_MIN_SPEED,
                        help="Minimum download speed in MB/s")
    parser.add_argument("-p", type=int, default=DEFAULT_PRINT_NUM,
                        help="Number of results to print (0 = don't print)")
    parser.add_argument("-f", type=str, default=DEFAULT_IP_FILE,
                        help="IP range file path")
    parser.add_argument("-ip", type=str, default="",
                        help="IP ranges (comma-separated, overrides -f)")
    parser.add_argument("-o", type=str, default=DEFAULT_OUTPUT_FILE,
                        help="Output CSV file path (empty = no file)")
    parser.add_argument("-oj", type=str, default="",
                        help="Output JSON file path (empty = no file)")
    parser.add_argument("-dd", action="store_true",
                        help="Disable download test")
    parser.add_argument("-allip", action="store_true",
                        help="Test all IPs in range (IPv4 only)")
    parser.add_argument("-debug", action="store_true",
                        help="Enable debug output")
    parser.add_argument("-v", "--version", action="store_true",
                        help="Print version and exit")
    
    return parser.parse_args()


async def main():
    args = parse_args()
    
    if args.version:
        print(f"CloudflareSpeedTest Python v{VERSION}")
        return
    
    # Validate arguments
    if args.n <= 0:
        args.n = DEFAULT_ROUTINES
    
    # Parse colo filter
    colo_filter = set()
    if args.cfcolo:
        colo_filter = {c.strip().upper() for c in args.cfcolo.split(",") if c.strip()}
        if not args.httping:
            print("Warning: -cfcolo requires -httping to be effective")
    
    # Load and generate IPs
    print("Loading IP ranges...")
    cidrs = load_ip_ranges(args.f, args.ip)
    if not cidrs:
        print("Error: No IP ranges found")
        return
    
    print("Generating IPs...")
    ips = generate_ips(cidrs, args.allip)
    if not ips:
        print("Error: No IPs generated")
        return
    
    print(f"Testing {len(ips)} IPs...")
    
    # Test latency
    ping_results = await test_latency(
        ips=ips,
        port=args.tp,
        ping_times=args.t,
        routines=args.n,
        use_httping=args.httping,
        url=args.url,
        httping_code=args.httping_code,
        colo_filter=colo_filter,
        debug=args.debug
    )
    
    if not ping_results:
        print("No IPs responded to latency test")
        return
    
    # Filter and sort
    ping_results = filter_by_delay(ping_results, args.tll, args.tl)
    ping_results = filter_by_loss_rate(ping_results, args.tlr)
    ping_results = sort_ping_results(ping_results)
    
    if not ping_results:
        print("No IPs passed the filters")
        return
    
    print(f"{len(ping_results)} IPs passed latency test")
    
    # Test download speed
    has_download = not args.dd
    if has_download:
        speed_results = await test_download_speed(
            ping_results=ping_results,
            url=args.url,
            port=args.tp,
            download_time=args.dt,
            test_count=args.dn,
            min_speed=args.sl,
            debug=args.debug
        )
    else:
        # Convert ping results to speed results without download
        speed_results = [
            SpeedResult(
                ip=r.ip,
                sent=r.sent,
                received=r.received,
                total_delay=r.total_delay,
                colo=r.colo
            )
            for r in ping_results
        ]
    
    if not speed_results:
        print("No results after speed test")
        return
    
    # Output results
    print_results(speed_results, args.p, has_download)
    export_csv(speed_results, args.o, has_download)
    export_json(speed_results, args.oj, has_download)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
