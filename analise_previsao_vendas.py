# 1. Importacao
from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeRegressor

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
sns.set_theme(style="whitegrid", palette="Set2")

try:
    from xgboost import XGBRegressor

    XGBOOST_AVAILABLE = True
except Exception:
    XGBOOST_AVAILABLE = False


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "outputs"
FIGURES_DIR = OUTPUT_DIR / "figures"
REPORTS_DIR = ROOT_DIR / "reports"

OUTPUT_DIR.mkdir(exist_ok=True)
FIGURES_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)


def brl(value: float) -> str:
    """Formata valore em padrao brasileiro."""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def save_plot(path: Path) -> None:
    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches="tight")
    plt.close()


def dataframe_to_markdown(df: pd.DataFrame, index: bool = False) -> str:
    return df.to_markdown(index=index)


# 2. Carregamento dos dados
def load_online_retail_files(data_dir: Path) -> tuple[pd.DataFrame, dict]:
    excel_files = sorted(data_dir.glob("*.xlsx")) + sorted(data_dir.glob("*.xls"))
    csv_files = sorted(data_dir.glob("*.csv"))

    if not excel_files and not csv_files:
        raise FileNotFoundError(
            "Nenhum arquivo .xlsx, .xls ou .csv foi encontrado na pasta data."
        )

    frames = []
    sources = []

    for file_path in excel_files:
        excel = pd.ExcelFile(file_path)
        for sheet_name in excel.sheet_names:
            frame = pd.read_excel(file_path, sheet_name=sheet_name)
            frame["source_file"] = file_path.name
            frame["source_sheet"] = sheet_name
            frames.append(frame)
            sources.append(
                {
                    "arquivo": file_path.name,
                    "aba": sheet_name,
                    "linhas": int(frame.shape[0]),
                    "colunas": int(frame.shape[1] - 2),
                }
            )

    for file_path in csv_files:
        frame = pd.read_csv(file_path, encoding="latin1")
        frame["source_file"] = file_path.name
        frame["source_sheet"] = "CSV"
        frames.append(frame)
        sources.append(
            {
                "arquivo": file_path.name,
                "aba": "CSV",
                "linhas": int(frame.shape[0]),
                "colunas": int(frame.shape[1] - 2),
            }
        )

    data = pd.concat(frames, ignore_index=True)
    return data, {"fontes": sources}


raw_data, source_info = load_online_retail_files(DATA_DIR)

# Padronizacao de nomes para reduzir espacos e diferencas de grafia.
raw_data = raw_data.rename(
    columns={
        "Customer ID": "CustomerID",
        "UnitPrice": "Price",
        "InvoiceNo": "Invoice",
    }
)

# 3. Analise inicial
initial_shape = raw_data.shape
initial_dtypes = raw_data.dtypes.astype(str).reset_index()
initial_dtypes.columns = ["coluna", "tipo"]
missing_values = (
    raw_data.isna()
    .sum()
    .reset_index()
    .rename(columns={"index": "coluna", 0: "nulos"})
)
missing_values["percentual"] = (
    missing_values["nulos"] / len(raw_data) * 100
).round(2)
duplicate_count = int(raw_data.duplicated().sum())
descriptive_stats = raw_data.describe(include="all")
sample_rows = raw_data.head(10)

initial_dtypes.to_csv(OUTPUT_DIR / "tipos_dados.csv", index=False)
missing_values.to_csv(OUTPUT_DIR / "valores_nulos.csv", index=False)
descriptive_stats.to_csv(OUTPUT_DIR / "estatisticas_descritivas.csv")
sample_rows.to_csv(OUTPUT_DIR / "amostra_dados.csv", index=False)

# 4. Limpeza
clean_data = raw_data.copy()
cleaning_log = []

before = len(clean_data)
clean_data["Invoice"] = clean_data["Invoice"].astype(str).str.strip()
clean_data["StockCode"] = clean_data["StockCode"].astype(str).str.strip()
clean_data["Description"] = clean_data["Description"].astype(str).str.strip()
clean_data["Country"] = clean_data["Country"].astype(str).str.strip()
clean_data["InvoiceDate"] = pd.to_datetime(clean_data["InvoiceDate"], errors="coerce")
cleaning_log.append(
    {
        "etapa": "Conversao de datas e padronizacao textual",
        "removidos": 0,
        "restantes": int(len(clean_data)),
        "justificativa": "Garante tipos consistentes para filtros, agregacoes temporais e analises por categorias.",
    }
)

before = len(clean_data)
clean_data = clean_data.drop_duplicates()
cleaning_log.append(
    {
        "etapa": "Remocao de duplicados",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Registros repetidos superestimam quantidades e faturamento.",
    }
)

before = len(clean_data)
clean_data = clean_data.dropna(subset=["Invoice", "StockCode", "InvoiceDate", "Quantity", "Price"])
cleaning_log.append(
    {
        "etapa": "Remocao de nulos criticos",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Sem invoice, produto, data, quantidade ou preco nao e possivel calcular uma venda confiavel.",
    }
)

before = len(clean_data)
clean_data = clean_data[~clean_data["Invoice"].str.upper().str.startswith("C")]
cleaning_log.append(
    {
        "etapa": "Remocao de devolucoes/cancelamentos",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Invoices iniciadas por C representam cancelamentos e nao devem compor a demanda vendida.",
    }
)

before = len(clean_data)
clean_data = clean_data[clean_data["Quantity"] > 0]
cleaning_log.append(
    {
        "etapa": "Remocao de quantidades negativas ou zero",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Quantidade menor ou igual a zero indica ajuste, erro ou devolucao, nao uma venda positiva.",
    }
)

before = len(clean_data)
clean_data = clean_data[clean_data["Price"] > 0]
cleaning_log.append(
    {
        "etapa": "Remocao de precos negativos ou iguais a zero",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Preco nao positivo inviabiliza o calculo de faturamento real.",
    }
)

before = len(clean_data)
clean_data = clean_data[clean_data["Description"].str.lower().ne("nan")]
cleaning_log.append(
    {
        "etapa": "Remocao de descricoes ausentes",
        "removidos": int(before - len(clean_data)),
        "restantes": int(len(clean_data)),
        "justificativa": "Descricoes ausentes dificultam analises de produtos mais vendidos e rentaveis.",
    }
)

clean_data["CustomerID"] = pd.to_numeric(clean_data["CustomerID"], errors="coerce")
clean_data["TotalSale"] = clean_data["Quantity"] * clean_data["Price"]
clean_data = clean_data.sort_values("InvoiceDate").reset_index(drop=True)

q1, q3 = clean_data["TotalSale"].quantile([0.25, 0.75])
iqr = q3 - q1
outlier_limit = q3 + 1.5 * iqr
line_outliers = int((clean_data["TotalSale"] > outlier_limit).sum())

cleaning_log_df = pd.DataFrame(cleaning_log)
cleaning_log_df.to_csv(OUTPUT_DIR / "log_limpeza.csv", index=False)
try:
    clean_data.to_parquet(OUTPUT_DIR / "dados_limpos.parquet", index=False)
    clean_data_artifact = "outputs/dados_limpos.parquet"
except Exception:
    clean_data.to_csv(OUTPUT_DIR / "dados_limpos.csv", index=False)
    clean_data_artifact = "outputs/dados_limpos.csv"

# 5. Engenharia de atributos
clean_data["Year"] = clean_data["InvoiceDate"].dt.year
clean_data["Month"] = clean_data["InvoiceDate"].dt.month
clean_data["Day"] = clean_data["InvoiceDate"].dt.day
clean_data["DayOfWeek"] = clean_data["InvoiceDate"].dt.dayofweek
clean_data["DayName"] = clean_data["InvoiceDate"].dt.day_name()
clean_data["Hour"] = clean_data["InvoiceDate"].dt.hour
clean_data["Date"] = clean_data["InvoiceDate"].dt.date
clean_data["YearMonth"] = clean_data["InvoiceDate"].dt.to_period("M").astype(str)
clean_data["WeekOfYear"] = clean_data["InvoiceDate"].dt.isocalendar().week.astype(int)

customer_agg = (
    clean_data.dropna(subset=["CustomerID"])
    .groupby("CustomerID")
    .agg(
        total_revenue=("TotalSale", "sum"),
        total_quantity=("Quantity", "sum"),
        invoices=("Invoice", "nunique"),
        distinct_products=("StockCode", "nunique"),
        avg_ticket=("TotalSale", "mean"),
        first_purchase=("InvoiceDate", "min"),
        last_purchase=("InvoiceDate", "max"),
    )
    .reset_index()
)
customer_agg["customer_lifetime_days"] = (
    customer_agg["last_purchase"] - customer_agg["first_purchase"]
).dt.days

daily_sales = (
    clean_data.groupby(pd.Grouper(key="InvoiceDate", freq="D"))
    .agg(
        daily_revenue=("TotalSale", "sum"),
        daily_quantity=("Quantity", "sum"),
        invoice_count=("Invoice", "nunique"),
        customer_count=("CustomerID", "nunique"),
        product_count=("StockCode", "nunique"),
        avg_unit_price=("Price", "mean"),
    )
    .reset_index()
    .rename(columns={"InvoiceDate": "date"})
)

all_dates = pd.date_range(daily_sales["date"].min(), daily_sales["date"].max(), freq="D")
daily_sales = (
    daily_sales.set_index("date")
    .reindex(all_dates)
    .rename_axis("date")
    .reset_index()
)
for column in ["daily_revenue", "daily_quantity", "invoice_count", "customer_count", "product_count"]:
    daily_sales[column] = daily_sales[column].fillna(0)
daily_sales["avg_unit_price"] = daily_sales["avg_unit_price"].ffill().bfill()

daily_sales["year"] = daily_sales["date"].dt.year
daily_sales["month"] = daily_sales["date"].dt.month
daily_sales["day"] = daily_sales["date"].dt.day
daily_sales["day_of_week"] = daily_sales["date"].dt.dayofweek
daily_sales["week_of_year"] = daily_sales["date"].dt.isocalendar().week.astype(int)
daily_sales["is_weekend"] = daily_sales["day_of_week"].isin([5, 6]).astype(int)
daily_sales["quarter"] = daily_sales["date"].dt.quarter

for lag in [1, 7, 14, 28]:
    daily_sales[f"revenue_lag_{lag}"] = daily_sales["daily_revenue"].shift(lag)
for window in [7, 14, 28]:
    daily_sales[f"revenue_roll_mean_{window}"] = (
        daily_sales["daily_revenue"].shift(1).rolling(window=window).mean()
    )
    daily_sales[f"revenue_roll_std_{window}"] = (
        daily_sales["daily_revenue"].shift(1).rolling(window=window).std()
    )

daily_model_data = daily_sales.dropna().reset_index(drop=True)
customer_agg.to_csv(OUTPUT_DIR / "agregacoes_clientes.csv", index=False)
daily_sales.to_csv(OUTPUT_DIR / "vendas_diarias.csv", index=False)
daily_model_data.to_csv(OUTPUT_DIR / "base_modelagem_diaria.csv", index=False)

# 6. EDA
top_products_qty = (
    clean_data.groupby("Description")["Quantity"]
    .sum()
    .sort_values(ascending=False)
    .head(15)
    .reset_index()
)
top_countries_revenue = (
    clean_data.groupby("Country")["TotalSale"]
    .sum()
    .sort_values(ascending=False)
    .head(15)
    .reset_index()
)
monthly_revenue = (
    clean_data.groupby("YearMonth")["TotalSale"].sum().reset_index(name="revenue")
)
weekday_revenue = (
    clean_data.groupby("DayName")["TotalSale"].sum().reset_index(name="revenue")
)
weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
weekday_revenue["DayName"] = pd.Categorical(
    weekday_revenue["DayName"], categories=weekday_order, ordered=True
)
weekday_revenue = weekday_revenue.sort_values("DayName")
top_customers = (
    customer_agg.sort_values("total_revenue", ascending=False)
    .head(15)[["CustomerID", "total_revenue", "invoices", "distinct_products"]]
)
top_profitable_products = (
    clean_data.groupby("Description")
    .agg(revenue=("TotalSale", "sum"), quantity=("Quantity", "sum"))
    .sort_values("revenue", ascending=False)
    .head(15)
    .reset_index()
)

top_products_qty.to_csv(OUTPUT_DIR / "produtos_mais_vendidos.csv", index=False)
top_countries_revenue.to_csv(OUTPUT_DIR / "paises_maior_faturamento.csv", index=False)
monthly_revenue.to_csv(OUTPUT_DIR / "faturamento_mensal.csv", index=False)
weekday_revenue.to_csv(OUTPUT_DIR / "faturamento_dia_semana.csv", index=False)
top_customers.to_csv(OUTPUT_DIR / "clientes_maior_faturamento.csv", index=False)
top_profitable_products.to_csv(OUTPUT_DIR / "produtos_mais_rentaveis.csv", index=False)

plt.figure(figsize=(10, 5))
sns.histplot(clean_data["TotalSale"].clip(upper=clean_data["TotalSale"].quantile(0.99)), bins=50)
plt.title("Distribuicao do valor das vendas por item (ate P99)")
plt.xlabel("Valor total do item")
plt.ylabel("Frequencia")
save_plot(FIGURES_DIR / "distribuicao_vendas.png")

plt.figure(figsize=(11, 6))
sns.barplot(data=top_products_qty, x="Quantity", y="Description")
plt.title("Produtos mais vendidos por quantidade")
plt.xlabel("Quantidade vendida")
plt.ylabel("Produto")
save_plot(FIGURES_DIR / "produtos_mais_vendidos.png")

plt.figure(figsize=(11, 6))
sns.barplot(data=top_countries_revenue, x="TotalSale", y="Country")
plt.title("Paises com maior faturamento")
plt.xlabel("Faturamento")
plt.ylabel("Pais")
save_plot(FIGURES_DIR / "paises_maior_faturamento.png")

plt.figure(figsize=(12, 5))
sns.lineplot(data=monthly_revenue, x="YearMonth", y="revenue", marker="o")
plt.title("Faturamento por mes")
plt.xlabel("Mes")
plt.ylabel("Faturamento")
plt.xticks(rotation=60)
save_plot(FIGURES_DIR / "faturamento_mensal.png")

plt.figure(figsize=(9, 5))
sns.barplot(data=weekday_revenue, x="DayName", y="revenue")
plt.title("Faturamento por dia da semana")
plt.xlabel("Dia da semana")
plt.ylabel("Faturamento")
plt.xticks(rotation=35)
save_plot(FIGURES_DIR / "faturamento_dia_semana.png")

plt.figure(figsize=(11, 6))
sns.barplot(data=top_customers, x="total_revenue", y=top_customers["CustomerID"].astype(str))
plt.title("Clientes com maior faturamento")
plt.xlabel("Faturamento")
plt.ylabel("Cliente")
save_plot(FIGURES_DIR / "faturamento_clientes.png")

plt.figure(figsize=(11, 6))
sns.barplot(data=top_profitable_products, x="revenue", y="Description")
plt.title("Produtos mais rentaveis")
plt.xlabel("Faturamento")
plt.ylabel("Produto")
save_plot(FIGURES_DIR / "produtos_mais_rentaveis.png")

plt.figure(figsize=(8, 5))
sns.boxplot(x=clean_data["TotalSale"].clip(upper=clean_data["TotalSale"].quantile(0.99)))
plt.title("Boxplot do valor total por item (ate P99)")
plt.xlabel("Valor total do item")
save_plot(FIGURES_DIR / "boxplot_vendas.png")

plt.figure(figsize=(10, 5))
sns.histplot(daily_sales["daily_revenue"], bins=40)
plt.title("Histograma do faturamento diario")
plt.xlabel("Faturamento diario")
plt.ylabel("Frequencia")
save_plot(FIGURES_DIR / "histograma_faturamento_diario.png")

correlation_columns = [
    "Quantity",
    "Price",
    "TotalSale",
    "Month",
    "DayOfWeek",
    "Hour",
    "WeekOfYear",
]
correlation_matrix = clean_data[correlation_columns].corr(numeric_only=True)
correlation_matrix.to_csv(OUTPUT_DIR / "correlacao_variaveis.csv")
plt.figure(figsize=(8, 6))
sns.heatmap(correlation_matrix, annot=True, fmt=".2f", cmap="coolwarm", center=0)
plt.title("Heatmap de correlacao")
save_plot(FIGURES_DIR / "heatmap_correlacao.png")

plt.figure(figsize=(13, 5))
sns.lineplot(data=daily_sales, x="date", y="daily_revenue")
plt.title("Serie temporal do faturamento diario")
plt.xlabel("Data")
plt.ylabel("Faturamento diario")
save_plot(FIGURES_DIR / "serie_temporal_faturamento_diario.png")

# 7. Modelagem
target = "daily_revenue"
feature_columns = [
    "daily_quantity",
    "invoice_count",
    "customer_count",
    "product_count",
    "avg_unit_price",
    "year",
    "month",
    "day",
    "day_of_week",
    "week_of_year",
    "is_weekend",
    "quarter",
    "revenue_lag_1",
    "revenue_lag_7",
    "revenue_lag_14",
    "revenue_lag_28",
    "revenue_roll_mean_7",
    "revenue_roll_std_7",
    "revenue_roll_mean_14",
    "revenue_roll_std_14",
    "revenue_roll_mean_28",
    "revenue_roll_std_28",
]

X = daily_model_data[feature_columns]
y = daily_model_data[target]
split_index = int(len(daily_model_data) * 0.8)
X_train, X_test = X.iloc[:split_index], X.iloc[split_index:]
y_train, y_test = y.iloc[:split_index], y.iloc[split_index:]

numeric_preprocess = Pipeline(
    steps=[
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
    ]
)
tree_preprocess = Pipeline(steps=[("imputer", SimpleImputer(strategy="median"))])

linear_pipeline = Pipeline(
    steps=[
        ("preprocess", numeric_preprocess),
        ("model", LinearRegression()),
    ]
)

models = {
    "Linear Regression": {
        "pipeline": linear_pipeline,
        "params": {},
    },
    "Decision Tree Regressor": {
        "pipeline": Pipeline(
            steps=[
                ("preprocess", tree_preprocess),
                ("model", DecisionTreeRegressor(random_state=42)),
            ]
        ),
        "params": {
            "model__max_depth": [3, 5, 8, None],
            "model__min_samples_leaf": [1, 3, 5, 10],
        },
    },
    "Random Forest Regressor": {
        "pipeline": Pipeline(
            steps=[
                ("preprocess", tree_preprocess),
                (
                    "model",
                    RandomForestRegressor(
                        random_state=42,
                        n_estimators=250,
                        n_jobs=1,
                    ),
                ),
            ]
        ),
        "params": {
            "model__max_depth": [5, 10, None],
            "model__min_samples_leaf": [1, 3, 5],
        },
    },
    "Gradient Boosting Regressor": {
        "pipeline": Pipeline(
            steps=[
                ("preprocess", tree_preprocess),
                ("model", GradientBoostingRegressor(random_state=42)),
            ]
        ),
        "params": {
            "model__n_estimators": [100, 200],
            "model__learning_rate": [0.05, 0.1],
            "model__max_depth": [2, 3],
        },
    },
}

if XGBOOST_AVAILABLE:
    models["XGBoost Regressor"] = {
        "pipeline": Pipeline(
            steps=[
                ("preprocess", tree_preprocess),
                (
                    "model",
                    XGBRegressor(
                        objective="reg:squarederror",
                        random_state=42,
                        n_estimators=250,
                        n_jobs=1,
                    ),
                ),
            ]
        ),
        "params": {
            "model__max_depth": [2, 3, 5],
            "model__learning_rate": [0.03, 0.08, 0.1],
            "model__subsample": [0.8, 1.0],
        },
    }

tscv = TimeSeriesSplit(n_splits=5)
model_results = []
best_estimators = {}

for model_name, model_info in models.items():
    if model_info["params"]:
        search = GridSearchCV(
            estimator=model_info["pipeline"],
            param_grid=model_info["params"],
            cv=tscv,
            scoring="neg_root_mean_squared_error",
            n_jobs=1,
        )
        fitted_model = search.fit(X_train, y_train)
        estimator = fitted_model.best_estimator_
        best_params = fitted_model.best_params_
        cv_rmse = -fitted_model.best_score_
    else:
        estimator = model_info["pipeline"].fit(X_train, y_train)
        cv_scores = []
        for train_idx, valid_idx in tscv.split(X_train):
            fold_model = model_info["pipeline"].fit(
                X_train.iloc[train_idx], y_train.iloc[train_idx]
            )
            fold_pred = fold_model.predict(X_train.iloc[valid_idx])
            cv_scores.append(math.sqrt(mean_squared_error(y_train.iloc[valid_idx], fold_pred)))
        best_params = {}
        cv_rmse = float(np.mean(cv_scores))

    predictions = estimator.predict(X_test)
    mae = mean_absolute_error(y_test, predictions)
    mse = mean_squared_error(y_test, predictions)
    rmse = math.sqrt(mse)
    r2 = r2_score(y_test, predictions)

    model_results.append(
        {
            "modelo": model_name,
            "MAE": mae,
            "MSE": mse,
            "RMSE": rmse,
            "R2": r2,
            "CV_RMSE": cv_rmse,
            "melhores_parametros": json.dumps(best_params),
        }
    )
    best_estimators[model_name] = estimator

results_df = pd.DataFrame(model_results).sort_values("RMSE").reset_index(drop=True)
results_df.to_csv(OUTPUT_DIR / "comparacao_modelos.csv", index=False)
best_model_name = results_df.iloc[0]["modelo"]
best_model = best_estimators[best_model_name]
best_predictions = best_model.predict(X_test)

prediction_df = pd.DataFrame(
    {
        "date": daily_model_data.loc[X_test.index, "date"].values,
        "real": y_test.values,
        "previsto": best_predictions,
    }
)
prediction_df.to_csv(OUTPUT_DIR / "previsoes_melhor_modelo.csv", index=False)

plt.figure(figsize=(12, 5))
plt.plot(prediction_df["date"], prediction_df["real"], label="Real", linewidth=2)
plt.plot(prediction_df["date"], prediction_df["previsto"], label="Previsto", linewidth=2)
plt.title(f"Valores reais versus previstos - {best_model_name}")
plt.xlabel("Data")
plt.ylabel("Faturamento diario")
plt.legend()
save_plot(FIGURES_DIR / "real_vs_previsto.png")

plt.figure(figsize=(9, 5))
sns.barplot(data=results_df, x="RMSE", y="modelo")
plt.title("Comparacao dos modelos por RMSE")
plt.xlabel("RMSE")
plt.ylabel("Modelo")
save_plot(FIGURES_DIR / "comparacao_modelos_rmse.png")

if hasattr(best_model.named_steps["model"], "feature_importances_"):
    importances = pd.DataFrame(
        {
            "atributo": feature_columns,
            "importancia": best_model.named_steps["model"].feature_importances_,
        }
    ).sort_values("importancia", ascending=False)
    importances.to_csv(OUTPUT_DIR / "importancia_atributos.csv", index=False)
    plt.figure(figsize=(10, 6))
    sns.barplot(data=importances.head(15), x="importancia", y="atributo")
    plt.title(f"Importancia dos atributos - {best_model_name}")
    plt.xlabel("Importancia")
    plt.ylabel("Atributo")
    save_plot(FIGURES_DIR / "importancia_atributos.png")
else:
    importances = pd.DataFrame(columns=["atributo", "importancia"])

# 8. Avaliacao
metadata = {
    "linhas_iniciais": int(initial_shape[0]),
    "colunas_iniciais": int(initial_shape[1]),
    "linhas_limpas": int(clean_data.shape[0]),
    "colunas_limpas": int(clean_data.shape[1]),
    "duplicados_iniciais": duplicate_count,
    "periodo_inicio": str(clean_data["InvoiceDate"].min()),
    "periodo_fim": str(clean_data["InvoiceDate"].max()),
    "faturamento_total": float(clean_data["TotalSale"].sum()),
    "quantidade_total": int(clean_data["Quantity"].sum()),
    "clientes_unicos": int(clean_data["CustomerID"].nunique()),
    "produtos_unicos": int(clean_data["StockCode"].nunique()),
    "paises_unicos": int(clean_data["Country"].nunique()),
    "outliers_linha_total_iqr": line_outliers,
    "limite_outlier_iqr": float(outlier_limit),
    "dias_modelagem": int(len(daily_model_data)),
    "dias_treino": int(len(X_train)),
    "dias_teste": int(len(X_test)),
    "xgboost_disponivel": XGBOOST_AVAILABLE,
    "melhor_modelo": str(best_model_name),
}
with open(OUTPUT_DIR / "metadata.json", "w", encoding="utf-8") as file:
    json.dump(metadata, file, ensure_ascii=False, indent=2)

# 9. Conclusao - relatorio em Markdown
results_report = results_df.copy()
for col in ["MAE", "MSE", "RMSE", "CV_RMSE"]:
    results_report[col] = results_report[col].map(lambda x: f"{x:,.2f}")
results_report["R2"] = results_report["R2"].map(lambda x: f"{x:.4f}")

top_products_report = top_products_qty.head(8).copy()
top_countries_report = top_countries_revenue.head(8).copy()
top_customers_report = top_customers.head(8).copy()
top_profitable_report = top_profitable_products.head(8).copy()

top_countries_report["TotalSale"] = top_countries_report["TotalSale"].map(brl)
top_customers_report["total_revenue"] = top_customers_report["total_revenue"].map(brl)
top_profitable_report["revenue"] = top_profitable_report["revenue"].map(brl)

cleaning_report = cleaning_log_df.copy()

report = f"""# Previsao de Vendas - Online Retail II

## Introducao

Este projeto aplica o processo CRISP-DM para analisar e prever vendas de uma empresa de varejo online usando o dataset Online Retail II armazenado localmente na pasta `data`. O arquivo identificado foi `online_retail_II.xlsx`, com as abas `Year 2009-2010` e `Year 2010-2011`, consolidadas em uma unica base analitica.

## Objetivo

O objetivo de negocio e apoiar decisoes de planejamento comercial, estoque e acompanhamento de receita por meio da previsao do faturamento diario. A variavel-alvo escolhida foi `daily_revenue`, criada pela agregacao diaria de `Quantity x Price`.

Perguntas orientadoras:

- Quais produtos, paises e clientes concentram maior faturamento?
- Como o faturamento varia ao longo do tempo, por mes e por dia da semana?
- Existem registros inconsistentes que podem distorcer a analise?
- Quais atributos ajudam a prever o faturamento diario?
- Qual modelo de regressao apresenta melhor desempenho para a previsao?

## Descricao da Base de Dados

A base consolidada iniciou com **{metadata["linhas_iniciais"]:,} linhas** e **{metadata["colunas_iniciais"]} colunas**. Apos a limpeza, restaram **{metadata["linhas_limpas"]:,} linhas**. O periodo analisado vai de **{metadata["periodo_inicio"]}** a **{metadata["periodo_fim"]}**.

Resumo apos limpeza:

- Faturamento total: **{brl(metadata["faturamento_total"])}**
- Quantidade total vendida: **{metadata["quantidade_total"]:,} unidades**
- Clientes unicos: **{metadata["clientes_unicos"]:,}**
- Produtos unicos: **{metadata["produtos_unicos"]:,}**
- Paises unicos: **{metadata["paises_unicos"]:,}**
- Duplicados identificados inicialmente: **{metadata["duplicados_iniciais"]:,}**

Arquivos e abas carregados:

{dataframe_to_markdown(pd.DataFrame(source_info["fontes"]))}

Tipos de dados identificados:

{dataframe_to_markdown(initial_dtypes)}

Valores nulos:

{dataframe_to_markdown(missing_values)}

## Metodologia CRISP-DM

### 1. Entendimento do Problema

O problema de negocio consiste em entender o comportamento historico das vendas e construir um modelo capaz de estimar faturamento futuro em nivel diario. Essa previsao pode orientar compras, reposicao de estoque, metas comerciais e alocacao operacional em periodos de maior demanda.

### 2. Entendimento dos Dados

Foram avaliadas dimensoes, tipos, valores nulos, duplicados, estatisticas descritivas e amostras. As tabelas completas foram salvas em `outputs/tipos_dados.csv`, `outputs/valores_nulos.csv`, `outputs/estatisticas_descritivas.csv` e `outputs/amostra_dados.csv`.

### 3. Preparacao dos Dados

As principais decisoes de limpeza foram:

{dataframe_to_markdown(cleaning_report)}

Os outliers de valor por item foram analisados com a regra do IQR. Foram encontrados **{metadata["outliers_linha_total_iqr"]:,}** itens acima de **{brl(metadata["limite_outlier_iqr"])}**. Eles foram mantidos na base principal porque pedidos grandes podem representar compras atacadistas reais; a decisao foi visualizar seu efeito em histogramas e boxplots com limite no percentil 99 para nao esconder o padrao central.

### 4. Analise Exploratoria

![Distribuicao das vendas](../outputs/figures/distribuicao_vendas.png)

A distribuicao do valor por item e assimetrica: muitos itens possuem baixo valor individual e poucos registros concentram valores altos. Isso e comum em varejo e reforca a necessidade de metricas robustas e modelos capazes de lidar com nao linearidade.

![Produtos mais vendidos](../outputs/figures/produtos_mais_vendidos.png)

Produtos mais vendidos por quantidade:

{dataframe_to_markdown(top_products_report)}

![Paises com maior faturamento](../outputs/figures/paises_maior_faturamento.png)

Paises com maior faturamento:

{dataframe_to_markdown(top_countries_report)}

O faturamento tende a ser concentrado no Reino Unido, caracteristica esperada para a base Online Retail II. Isso indica que analises globais podem refletir principalmente o comportamento desse mercado.

![Faturamento mensal](../outputs/figures/faturamento_mensal.png)

O grafico mensal mostra sazonalidade e variacao temporal. Meses proximos ao fim do ano tendem a ser relevantes em varejo por compras sazonais, o que justifica a criacao de variaveis de mes, semana e defasagens.

![Faturamento por dia da semana](../outputs/figures/faturamento_dia_semana.png)

A analise por dia da semana ajuda a identificar padroes operacionais e possiveis dias de menor atividade. Essa informacao foi incorporada na modelagem por `day_of_week` e `is_weekend`.

![Faturamento por cliente](../outputs/figures/faturamento_clientes.png)

Clientes com maior faturamento:

{dataframe_to_markdown(top_customers_report)}

![Produtos mais rentaveis](../outputs/figures/produtos_mais_rentaveis.png)

Produtos mais rentaveis:

{dataframe_to_markdown(top_profitable_report)}

![Boxplot das vendas](../outputs/figures/boxplot_vendas.png)

O boxplot evidencia a presenca de vendas por item acima do comportamento central. Elas foram analisadas, mas nao removidas automaticamente para preservar pedidos grandes potencialmente legitimos.

![Histograma do faturamento diario](../outputs/figures/histograma_faturamento_diario.png)

O faturamento diario tambem apresenta dispersao, com dias de movimento baixo e dias de receita elevada. Esse comportamento torna a comparacao de modelos necessaria.

![Heatmap de correlacao](../outputs/figures/heatmap_correlacao.png)

O heatmap mostra relacoes lineares entre quantidade, preco, valor total e variaveis temporais. Correlacoes baixas nao descartam utilidade preditiva, pois modelos de arvore capturam padroes nao lineares.

![Serie temporal do faturamento diario](../outputs/figures/serie_temporal_faturamento_diario.png)

A serie temporal mostra tendencia, sazonalidade e variabilidade diaria. Por isso, a divisao treino/teste foi temporal, preservando a ordem cronologica.

## Engenharia de Atributos

Foram criados os seguintes atributos:

- `TotalSale`: valor total do item, calculado por `Quantity x Price`.
- `Year`, `Month`, `Day`, `DayOfWeek`, `Hour`, `WeekOfYear`: capturam sazonalidade e ciclos de compra.
- `YearMonth`: facilita agregacoes mensais e analises temporais.
- Agregacoes por cliente: faturamento, quantidade, numero de invoices, diversidade de produtos, ticket medio e tempo de relacionamento.
- Agregacao diaria: faturamento, quantidade, invoices, clientes, produtos e preco medio por dia.
- Defasagens (`revenue_lag_1`, `revenue_lag_7`, `revenue_lag_14`, `revenue_lag_28`) e medias/desvios moveis: incorporam memoria temporal e sazonalidade semanal/mensal.

Essas variaveis melhoram a previsao porque transformam transacoes individuais em sinais temporais alinhados ao objetivo de estimar faturamento diario.

## Modelagem

Foram comparados modelos de regressao para valor continuo:

- Linear Regression
- Decision Tree Regressor
- Random Forest Regressor
- Gradient Boosting Regressor
{"- XGBoost Regressor" if XGBOOST_AVAILABLE else "- XGBoost nao foi executado porque a biblioteca nao estava disponivel no ambiente"}

A base de modelagem possui **{metadata["dias_modelagem"]} dias** apos a criacao das defasagens. A divisao foi temporal: **{metadata["dias_treino"]} dias** para treino e **{metadata["dias_teste"]} dias** para teste. A validacao cruzada usou `TimeSeriesSplit`, apropriado para dados temporais porque evita treinar com informacoes posteriores ao periodo validado.

## Resultados

Comparacao dos modelos:

{dataframe_to_markdown(results_report)}

![Comparacao dos modelos](../outputs/figures/comparacao_modelos_rmse.png)

O melhor modelo pelo menor RMSE no conjunto de teste foi **{best_model_name}**. A escolha pelo RMSE prioriza modelos que reduzem erros grandes, importantes em previsao de faturamento porque grandes desvios podem afetar planejamento de estoque e metas.

![Real versus previsto](../outputs/figures/real_vs_previsto.png)

O grafico de valores reais versus previstos permite avaliar visualmente se o modelo acompanha a direcao da serie e se falha em picos especificos.

"""

if not importances.empty:
    report += f"""
![Importancia dos atributos](../outputs/figures/importancia_atributos.png)

Os atributos mais importantes indicam quais sinais historicos e operacionais mais contribuiram para a previsao. Em modelos de arvore, defasagens e agregacoes recentes costumam ser relevantes porque capturam continuidade temporal.

"""

best_row = results_df.iloc[0]
report += f"""## Conclusao

O projeto consolidou as duas abas do Online Retail II, removeu registros inconsistentes, criou variaveis temporais e agregacoes de negocio, realizou EDA com graficos interpretaveis e comparou modelos de regressao para prever faturamento diario.

Principais descobertas:

- O faturamento e concentrado em poucos paises, produtos e clientes.
- A distribuicao de vendas e assimetrica, com muitos itens de baixo valor e poucos itens de valor elevado.
- Variaveis temporais e defasagens sao importantes para representar padroes de venda.
- A avaliacao temporal e mais adequada que uma divisao aleatoria para esse problema.

Melhor modelo: **{best_model_name}**

- MAE: **{best_row["MAE"]:,.2f}**
- RMSE: **{best_row["RMSE"]:,.2f}**
- R2: **{best_row["R2"]:.4f}**

Limitacoes:

- O dataset contem apenas historico transacional; nao ha informacoes externas como campanhas, feriados, trafego do site ou ruptura de estoque.
- A previsao diaria usa agregacoes historicas, portanto nao explica integralmente eventos comerciais pontuais.
- Pedidos muito grandes foram mantidos por poderem ser reais, mas eles aumentam a dificuldade de prever picos.

Melhorias futuras:

- Incluir calendario de feriados e eventos promocionais.
- Testar modelos especificos de series temporais, como SARIMAX, Prophet ou LightGBM com features temporais.
- Criar previsoes por pais, categoria ou produto para apoiar decisoes mais granulares.
- Avaliar estrategias de tratamento de outliers por segmento, em vez de uma regra global.

## Artefatos Gerados

- Dados limpos: `{clean_data_artifact}`
- Base diaria de modelagem: `outputs/base_modelagem_diaria.csv`
- Comparacao dos modelos: `outputs/comparacao_modelos.csv`
- Previsoes do melhor modelo: `outputs/previsoes_melhor_modelo.csv`
- Graficos: `outputs/figures/`
- Relatorio: `reports/relatorio.md`
"""

with open(REPORTS_DIR / "relatorio.md", "w", encoding="utf-8") as file:
    file.write(report)

print("Projeto concluido.")
print(f"Relatorio gerado em: {REPORTS_DIR / 'relatorio.md'}")
print(f"Melhor modelo: {best_model_name}")
print(results_df[["modelo", "MAE", "RMSE", "R2", "CV_RMSE"]].to_string(index=False))
