# 이마트 와인장터 가격 비교

이마트 와인장터 A·B등급 매장 행사가를 **에이와인 공식 판매가**와 Google 검색 결과에 표시된 Wine-Searcher의 **세전 750mL 평균가**에 비교합니다.

## 에이와인 공식 판매가 갱신

Python 의존성을 설치하고 `curl`을 사용할 수 있는 환경에서 실행하세요.

```powershell
python -m pip install -r requirements.txt
python awine_prices.py --cache .work/awine-YYYYMMDD
```

공식 와인 사이트맵의 상품 주소를 전체 행사 목록과 대조한 뒤 상품 상세의 이름, 빈티지, 규격, 원화 가격, 재고 표시를 읽습니다. 동일 빈티지를 우선하며 가까운 빈티지와 750mL 가정은 참고 조건으로 표시합니다. 같은 캐시로 재실행하면 이미 읽은 상품을 건너뜁니다. 새로 조회할 때는 새 날짜의 캐시를 지정하세요. `--cached-only`로 저장된 상품 상세를 다시 반영할 수 있습니다.

정렬 메뉴에서는 에이와인 판매가에서 이마트 일반 행사가 또는 카드가를 뺀 차이를 큰순과 작은순으로 확인할 수 있습니다. 여러 빈티지를 표시한 행에서는 첫 번째 가격을 정렬에 사용합니다. CSV에도 상품 링크, 재고 표시, 조회일과 차이를 담습니다.

## A·B등급 통합과 Google 검색

A와 B 원본 PDF를 각각 지정하세요. 상품·빈티지·용량·행사가·행사 조건이 일치하는 행은 합치고, 원본 PDF의 등급과 행 위치를 각각 남깁니다. 기존 대시보드의 가격과 분류는 `--base`에서 읽습니다. `--fx`로 환율을 지정할 수 있으며, 생략하면 기준 대시보드에 표시된 환율을 사용합니다.

```powershell
python -m pip install -r requirements.txt playwright
python -m playwright install chromium
$a = 'input/A 26-1030_와인장터 리플렛A5_48p.pdf'
$b = 'input/2026 하반기 이마트 와인장터 리스트.pdf'
python market_catalog.py --grade-a $a --grade-b $b --output .work/catalog.html --query-plan .work/google-queries.json
python google_prices.py .work/google-queries.json --snippet-search
python market_catalog.py --grade-a $a --grade-b $b --observations .work/google-observations.jsonl
```

`--snippet-search`는 Wine-Searcher 사이트로 검색을 좁히고 상품명, 빈티지, `Avg Price` 문구를 검색합니다. 표기된 빈티지가 여러 개면 빈티지별로 조회합니다. 영문명은 `wine_aliases.tsv`와 `grade_catalog.tsv`를 사용합니다.

수집기는 일반 검색 결과의 `Avg Price (ex-tax)` 문구와 750mL 단위를 읽고, 상품명과 출처 주소를 확인합니다. 같은 상품의 동일 빈티지 일반 검색가를 우선 사용합니다. 다음 순위는 3년 이내에서 가장 가까운 빈티지, 빈티지 미지정 일반 검색가입니다. 일반 검색에서 해당하는 값이 없으면 확인 가능한 AI 개요 가격을 추정가로 표시합니다.

참고가는 행사 빈티지와 검색 빈티지를 함께 표시합니다. 행사 빈티지가 미표기된 상품은 같은 와인의 빈티지 미지정 평균가를 우선 사용하고, 해당 값이 없으면 확인된 최근 빈티지를 사용합니다. 전 세계 집계가 명시되지 않은 가격은 범위를 미확인으로 표시합니다. Google에서 자동 트래픽 확인 화면이 나오면 수집을 중단합니다. 다음 실행은 저장된 항목을 건너뜁니다.

평균가가 비어 있는 상품·빈티지만 일반 검색 결과로 다시 조회하려면 다음과 같이 실행하세요. 국가로 제한된 결과는 전 세계 평균이 명시된 경우에만 새 가격으로 채택합니다. 검색 결과가 없던 항목을 같은 저장 파일에서 다시 조회할 때는 `--retry-unpriced`를 추가할 수 있습니다.

```powershell
python market_catalog.py --refresh-page --unpriced-only --query-plan .work/missing-google-queries.json --output .work/catalog.html
python google_prices.py .work/missing-google-queries.json --snippet-search --output .work/missing-google-observations.jsonl
python market_catalog.py --refresh-page --observations .work/missing-google-observations.jsonl
```

현재 대시보드의 목록으로 가격만 갱신할 때는 다음 명령을 사용합니다. 브라우저에서 저장한 Google 검색 결과를 가져올 때는 `--captures`에 각 검색의 `name`, `vintage`, `url`, `checked_at`, `snapshot`이 담긴 JSONL 파일을 지정합니다. 조회한 항목은 가격 유무와 관계없이 검색 링크와 날짜를 보관합니다.

```powershell
python market_catalog.py --refresh-page --observations .work/google-observations.jsonl
python market_catalog.py --refresh-page --captures .work/google-captures.jsonl --workers 4
```

가격 차이는 검색가의 원화 환산액에서 일반 장터가를 뺍니다. 검색 가격 범위는 양 끝의 차이를 표시합니다. 가격이 있는 모든 판매 행은 차이를 계산하며, 복수·미표기 빈티지 또는 다른 행사 용량·구성은 참고 조건을 표시합니다. 정렬에서 카드 적용가와 별개로 이마트 일반 행사가의 낮은순과 높은순을 선택할 수 있습니다. CSV에는 등급과 출처, 가격 차이가 포함됩니다.

## 1. 원본 목록 확인

Python 3.10 이상이 필요합니다. 원본은 `input/` 폴더에 두세요.

```powershell
python -m pip install -r requirements.txt
python build_dashboard.py inspect 'input/2026 하반기 이마트 와인장터 리스트.pdf'
python build_dashboard.py inspect input/market.xlsx
```

2026년 하반기 이마트 와인장터 PDF는 각 쪽의 왼쪽·오른쪽 표를 분리해 상품 행을 가져옵니다. 일반 장터가, 카드 적용가, 교차구매 조건을 각각 표시합니다. PDF 가격표만 대시보드에 넣으려면 다음 명령을 실행하세요.

```powershell
python build_dashboard.py build 'input/2026 하반기 이마트 와인장터 리스트.pdf' --export-queries .work/queries.csv
```

시트에서 와인명과 이마트 행사가 열을 찾습니다. `빈티지`, `용량`, `Wine-Searcher 검색명` 열을 함께 두면 매칭에 사용합니다. 검색명은 Wine-Searcher에 표시되는 생산자와 큐베의 영문 전체 이름을 권장합니다. LWIN 코드를 쓰는 API 조회에서는 `LWIN`을 앞에 붙입니다.

헤더가 다르면 `--header-row 3 --wine-column '상품명' --price-column '이마트 행사 가격'`처럼 지정합니다. 여러 후보 시트가 나오면 원본 목록 시트 이름을 `--sheet '레드' --sheet '화이트'`처럼 지정합니다. 원본 시트의 데이터 행을 각각 남기므로 같은 와인이 여러 번 등장해도 행별 가격을 비교할 수 있습니다. 가격이 엑셀 수식이면 Excel에서 계산한 뒤 저장한 파일을 사용하세요.

PDF에 영문 조회명이 없거나 빈티지·용량이 불확실하면 `.work/queries.csv`의 `Product Name`, `Vintage`, `Bottle mL` 열을 확인한 값으로 채웁니다. 다시 실행할 때 `--lookup-map .work/queries.csv`를 지정하면 원본 행 참조 코드로 연결합니다. 복수 빈티지가 표기된 행에는 실제 판매 빈티지를 확인한 뒤 입력하세요.

## 2. Wine-Searcher 가격 가져오기

### 네이버 웹문서 검색 API

[NAVER API HUB 검색 API](https://api.ncloud-docs.com/docs/naver-api-hub-search-webkr)의 Client ID와 Client Secret을 사용합니다. `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` 환경변수가 비어 있으면 터미널에서 가려진 입력을 받습니다.

```powershell
python naver_prices.py --aliases wine_aliases.tsv
python naver_snapshot.py --aliases wine_aliases.tsv --fx .work/naver-fx.json
```

`naver_prices.py`는 대시보드의 원본 행을 읽고 검색어를 중복 제거해 `.work/naver-search.sqlite3`에 응답을 저장합니다. `--queries`로 줄마다 한 개씩 추가 검색어를 지정할 수 있습니다. `naver_snapshot.py`는 Wine-Searcher 상품 페이지 요약의 `Avg Price (ex-tax)`와 750mL 단위를 읽고, 상품명과 행사 빈티지가 맞는 값을 연결합니다. 빈티지가 미표기된 행은 상품 평균가와 조회된 빈티지를 참고로 표시합니다. `wine_aliases.tsv`는 한글 조회명에 대응하는 영문 검색어 목록입니다.

검색 요약의 `Worldwide` 기준이 확인된 값은 별도로 표시합니다. 행사 단품의 용량이 미표기된 경우에 한해 750mL 가정으로 참고 차이율을 계산합니다. 검색 요약의 가격 기준일이 미표시이므로 수집일을 따로 표시합니다. 외화 환산을 하려면 `--fx` 파일에 `as_of`, `source`, `krw_per_unit`을 입력합니다. 환율은 통화 1단위당 원화입니다. 예를 들어 `{"as_of":"YYYY-MM-DD","source":"ECB 기준환율","krw_per_unit":{"USD":"1346.788171"}}` 형태입니다.

### Wine Check API

[Wine-Searcher API](https://www.wine-searcher.com/trade/api)에서 발급받은 Wine Check 주소와 키가 필요합니다. 발급 조건에서 전 세계 750mL 세전 평균가가 제공되는지 확인하고, 한 건을 조회해 Wine-Searcher의 Analytics 값과 대조한 후 목록을 처리하세요. 공식 API 계정이 없는 상태에서는 연결과 응답 검증에 모의 응답을 사용했습니다.

Windows PowerShell에서 키는 현재 세션의 환경 변수로 전달합니다. PowerShell 7 이상에서는 입력 마스킹을 지원합니다.

```powershell
$env:WINE_SEARCHER_API_URL = Read-Host 'Wine Check API URL'
$env:WINE_SEARCHER_API_KEY = Read-Host 'Wine Check API key' -MaskInput
python wine_searcher.py 'Chateau Margaux' 2000 --confirm-global-basis
python build_dashboard.py build input/market.xlsx --api --confirm-global-basis
```

조회에 지역 제한을 넣지 않고 KRW를 요청합니다. 평균가, 통화, 와인명 또는 LWIN, 빈티지를 확인합니다. 결과가 애매하거나 평균가가 없으면 확인 필요로 표시합니다. 요청은 1초 이상 간격으로 보냅니다. `--max-requests 100`처럼 계정의 호출 한도를 지정할 수 있습니다. API 응답은 `.work/`에 기본 168시간 보관하며, 다시 실행하면 캐시가 없는 조회부터 진행합니다. `--cache-hours 0`은 이전 실행의 캐시를 무시합니다.

### Market Pricing Report

[Wine-Searcher 보고서의 `Global Avg Price`](https://www.wine-searcher.com/images/for-trade/220615_WineSearcher_MarketPricingReport_Sample.pdf)를 CSV 또는 엑셀로 가져올 수 있습니다. 보고서의 `Product Name`과 `Vintage`가 원본의 검색명과 빈티지에 일치해야 합니다. `--export-queries .work/queries.csv`를 지정하면 보고서 요청에 사용할 이름, 빈티지, 원본 행 참조 코드가 생성됩니다.

```powershell
python build_dashboard.py build input/market.xlsx --report input/wine-searcher.csv --report-currency KRW --report-as-of YYYY-MM-DD
```

외화 보고서라면 보고서 통화와 가격 기준일을 지정하고 `--fx-rate`에 해당 통화 1단위당 원화 환율, `--fx-source`에 환율 출처와 기준일을 입력합니다. 대시보드에 원본 외화와 원화 환산가가 함께 표시됩니다.

### 공식 API·보고서 비교 기준

- 할인율은 `(세계 평균가 − 일반 장터가) ÷ 세계 평균가 × 100`으로 계산합니다. 양수인 행을 구매 후보로 표시합니다. 카드 적용가와 다병 구매 조건은 함께 표시합니다.
- 빈티지가 모호하거나 행사가 또는 750mL 단품 여부가 확인되지 않으면 확인 필요에 남깁니다. 용량이 비어 있는 단품의 용량이 750mL임을 확인한 경우 `--assume-750ml`로 지정할 수 있습니다.
- 가격 소스 옵션을 생략하면 원본 행을 먼저 올리고 가격 수집 대기 상태를 확인할 수 있습니다.

## 3. 웹으로 보기와 GitHub Pages 게시

`build`가 만든 `docs/index.html`은 브라우저에서 바로 열 수 있습니다. 기본 파일의 **예시 보기**는 가상 와인과 가격으로 검색, 정렬, 구매 후보, 미확인 행을 보여줍니다.

GitHub에 소스를 올리고 [저장소의 Pages 설정](https://github.com/leerazor/wine-dashboard/settings/pages)에서 **Deploy from a branch → main → /docs → Save**를 선택합니다. GitHub Pages가 표시하는 게시 주소에서 확인합니다. 기본 주소는 [leerazor.github.io/wine-dashboard](https://leerazor.github.io/wine-dashboard/)입니다.

원본 PDF와 엑셀, 보고서, API 키와 수집 캐시는 로컬에 둡니다. `docs/index.html`에는 대시보드에 표시되는 데이터가 포함되므로, 가격 데이터의 게시 범위를 Wine-Searcher와 계약한 이용 조건에 맞춰 관리합니다.
