# PMData.dev + Nautilus Trader 数据摄入指南

使用 API key 从 https://pmdata.dev 下载 Polymarket tick 数据，并转换为 Nautilus Trader (PyO3) 格式写入 ParquetDataCatalog，用于回测。

## 1. 下载数据

**方式（pandas 最简单，已验证你的 key 可用）**：

```python
import pandas as pd

API_KEY = "sk-UW15uNF3oQGdbmTLbnNlGcQHq51UNAZt"
slug = "btc-updown-5m-1778803200"
data_type = "poly_l2"   # 推荐：含 book + price_change + last_trade_price

url = f"https://api.pmdata.dev/download/{data_type}/{slug}.parquet"
df = pd.read_parquet(
    url,
    storage_options={"api_key": API_KEY, "User-Agent": "Mozilla/5.0"}
)
print(df.head(), df["event_type"].value_counts())
```

支持的 data_type（从官网和代码推断）：
- `poly_l2`：最完整，tick-by-tick L2（book 快照 + 增量 + trades）
- `poly_snapshot`：可能只含快照

**获取 slug**：
- 访问 https://pmdata.dev/app 填入你的 key，浏览可用的 up/down 市场（7 个币种 × 5m/15m/1h）。
- slug 格式示例：`btc-updown-5m-1778803200`、`eth-updown-15m-...`

并发下载：官网提到支持较高并发（200 markets / 10s），可以用 ThreadPoolExecutor 批量下载多个 slug。

## 2. 项目已有 pyO3 回测支持（推荐先用这个）

本仓库 `pm-hftbacktest` 已经完美集成 pmdata.dev（官网还专门链接了这个项目）。

使用现成转换器：

```python
from hftbacktest import polymarket_to_hbt, BacktestAssetPoly, ROIVectorMarketDepthBacktest, ...
import pandas as pd

df = pd.read_parquet(...)  # 如上
data = polymarket_to_hbt(df)   # 直接转成内部事件数组（Rust + Numba）

asset = BacktestAssetPoly().data(data)
hbt = ROIVectorMarketDepthBacktest([asset])
...
```

详见：
- `py-hftbacktest/hftbacktest/data/utils/polymarket.py`
- `README.rst` （有完整尾盘策略示例）
- `example/`

这个路径针对 Polymarket 二元市场做了大量优化（ROI Vector 深度、结算价处理等），性能极高。

## 3. 转换为 Nautilus Trader 格式 + 写入 Catalog（本仓库新增）

我们创建了 [nautilus_pmdata_ingest.py](./nautilus_pmdata_ingest.py) 实现完整 pipeline：

### 特性
- 下载 + 转换（book → CLEAR + ADD deltas；price_change → UPDATE/DELETE；trades → TradeTick）
- 创建 `BinaryOption` instrument（价格 0~1）
- 使用当前 Nautilus（v2 dev）的专用 writer：
  - `write_instruments`
  - `write_order_book_deltas`
  - `write_trade_ticks`
- 支持查询验证

### 运行

```bash
# 安装依赖（在你的 Nautilus 环境）
pip install "nautilus_trader[polymarket]" pandas pyarrow

# 单 slug 完整摄入
python nautilus_pmdata_ingest.py \
    --slug btc-updown-5m-1778803200 \
    --data-type poly_l2 \
    --catalog ./nautilus_catalog

# 只下载原始 parquet
python nautilus_pmdata_ingest.py --slug ... --download-only
```

### 代码级使用

```python
from nautilus_pmdata_ingest import download_pmdata, pmdata_to_nautilus, ingest_to_catalog
from nautilus_trader.persistence import ParquetDataCatalog

df = download_pmdata("btc-updown-5m-1778803200")
inst, data = pmdata_to_nautilus(df)

# 或直接
cat = ingest_to_catalog(df, slug="btc-updown-5m-1778803200", catalog_path="./catalog")
```

生成对象数量说明：每个 `book` 行会展开为 ~1 CLEAR + N levels 的 deltas（数据里一般 40-50 档）。这是 tick L2 的正常规模。

### 回测中使用 Catalog

```python
from nautilus_trader.backtest.config import BacktestDataConfig, BacktestRunConfig, BacktestVenueConfig
from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.model.data import OrderBookDelta, TradeTick
from nautilus_trader.model import InstrumentId

catalog_path = "./nautilus_catalog"
instrument_id = "btc-updown-5m-1778803200.POLYMARKET"

data_configs = [
    BacktestDataConfig(
        catalog_path=catalog_path,
        data_cls=OrderBookDelta,
        instrument_id=InstrumentId.from_str(instrument_id),
        # start_time=..., end_time=...
    ),
    # 可同时加 TradeTick
]

# venue + strategies 配置...
node = BacktestNode(configs=[...])
results = node.run()
```

## 4. 建议

- **如果你主要做 Polymarket 高频/订单簿策略**：优先使用仓库自带的 `hftbacktest` pyO3 路径，数据直接转 event 数组即可，延迟模型、深度模型都针对性优化过。
- **如果你需要 Nautilus 全套生态**（多品种、风控、组合、与实盘统一、策略框架等）：使用上面新建的 ingest 脚本把数据灌进 Nautilus catalog。
- 大量数据时注意：
  - 先用小时间段测试转换正确性
  - Nautilus 支持按时间 range 加载
  - 考虑是否需要对 book 做降采样或只保留 10 档（有 OrderBookDepth10）

## 5. 数据字段速查（poly_l2）

- `book`：完整 L2 快照（bid/ask prices + sizes 数组）
- `price_change`：增量更新（pc_price / pc_size / pc_side）
- `last_trade_price`：成交（trade_price / trade_size / trade_side）
- `market_resolved` + `winning_outcome`：结算（脚本中已处理）

时间戳：
- `timestamp` = 交易所时间（ts_event）
- `local_timestamp` = 采集时间（ts_init）

---

有问题可以继续问我继续完善转换逻辑（例如支持 OrderBookDeltas 批量、QuoteTick 合成、不同精度、 settlement 处理等）。 
