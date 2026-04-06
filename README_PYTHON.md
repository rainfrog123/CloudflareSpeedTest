# CloudflareSpeedTest - Python Version

A Python implementation of CloudflareSpeedTest that measures latency and download speed to Cloudflare IPs and ranks the best ones.

## Features

- **IPv4 & IPv6 support** - Test both IPv4 and IPv6 addresses from CIDR ranges
- **TCP Ping** - Measure latency using TCP handshake (default)
- **HTTP Ping** - Optionally use HTTP HEAD requests for latency measurement
- **Download Speed Test** - Measure actual download throughput through each IP
- **Flexible Filtering** - Filter by latency range, packet loss, minimum speed
- **Colo Detection** - Detect Cloudflare datacenter location from response headers
- **Async I/O** - Uses asyncio for efficient concurrent testing

## Requirements

- Python 3.10+
- aiohttp

## Installation

```bash
pip install -r requirements.txt
```

## Usage

Basic usage (tests IPs from `ip.txt` file):
```bash
python cloudflare_speedtest.py
```

### Command Line Options

| Flag | Default | Description |
|------|---------|-------------|
| `-n` | 200 | Number of concurrent latency test routines |
| `-t` | 4 | Number of latency tests per IP |
| `-dn` | 10 | Number of IPs to test for download speed |
| `-dt` | 10 | Download test duration in seconds |
| `-tp` | 443 | TCP port for testing |
| `-url` | https://cf.xiu2.xyz/url | URL for HTTPing and download test |
| `-httping` | false | Use HTTP HEAD for latency test instead of TCP |
| `-httping-code` | 0 | Valid HTTP status code (0 = accept 200/301/302) |
| `-cfcolo` | "" | Filter by Cloudflare colo codes (comma-separated) |
| `-tl` | 9999 | Maximum average latency in ms |
| `-tll` | 0 | Minimum average latency in ms |
| `-tlr` | 1 | Maximum packet loss rate (0-1) |
| `-sl` | 0 | Minimum download speed in MB/s |
| `-p` | 10 | Number of results to print (0 = don't print) |
| `-f` | ip.txt | IP range file path |
| `-ip` | "" | IP ranges (comma-separated, overrides -f) |
| `-o` | result.csv | Output CSV file path (empty = no file) |
| `-dd` | false | Disable download test |
| `-allip` | false | Test all IPs in range (IPv4 only) |
| `-debug` | false | Enable debug output |
| `-v` | - | Print version and exit |

### Examples

Test with custom IP range:
```bash
python cloudflare_speedtest.py -ip "1.1.1.0/24,1.0.0.0/24"
```

Use HTTP ping instead of TCP:
```bash
python cloudflare_speedtest.py -httping
```

Filter for IPs with less than 100ms latency and at least 5 MB/s download:
```bash
python cloudflare_speedtest.py -tl 100 -sl 5
```

Skip download test (latency only):
```bash
python cloudflare_speedtest.py -dd
```

Filter by specific Cloudflare colos:
```bash
python cloudflare_speedtest.py -httping -cfcolo "LAX,SJC,SEA"
```

Test all IPs in small ranges:
```bash
python cloudflare_speedtest.py -allip -ip "1.1.1.0/28"
```

## IP File Format

The `ip.txt` file should contain one CIDR range per line:

```
1.0.0.0/24
1.1.1.0/24
104.16.0.0/12
# Lines starting with # are ignored
```

## Output

### Console Output

```
IP Address        Sent Received  Loss Rate    Avg Delay   Speed(MB/s)     Colo
----------------------------------------------------------------------
1.1.1.1              4        4      0.00%        12.34           5.67      LAX
1.0.0.1              4        4      0.00%        15.23           4.89      SJC
```

### CSV Output

Results are saved to `result.csv` (or custom path via `-o`) with columns:
- IP 地址 (IP Address)
- 已发送 (Sent)
- 已接收 (Received)
- 丢包率 (Loss Rate)
- 平均延迟 (Average Delay)
- 下载速度(MB/s) (Download Speed)
- 地区码 (Colo Code)

## How It Works

1. **IP Generation**: Parses CIDR ranges and generates test IPs. For large IPv4 ranges, randomly samples one IP per /24 subnet unless `-allip` is specified.

2. **Latency Testing**: Uses asyncio with a semaphore to limit concurrency. By default, performs TCP handshake timing. With `-httping`, sends HTTP HEAD requests instead.

3. **Filtering & Sorting**: Filters results by delay range and loss rate. Sorts by loss rate (ascending), then by delay (ascending).

4. **Download Testing**: For top candidates, downloads from the URL through each IP, measuring throughput using EWMA (Exponentially Weighted Moving Average).

5. **Output**: Displays results in a table and exports to CSV.

## Differences from Go Version

- Uses Python's asyncio instead of goroutines
- Uses aiohttp for HTTP operations
- Identical command-line interface and defaults
- Same filtering and sorting logic
- Compatible CSV output format
