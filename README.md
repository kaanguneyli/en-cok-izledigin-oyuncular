# En Çok İzlediğin Oyuncular

Herkese açık bir Letterboxd profilindeki izlenen filmleri ve günlük kayıtlarını okuyup en çok izlenen oyuncuları sıralar. Tekrar izlemeleri hesaba katar, film bazında filtreleme yapar ve sonucu Excel olarak dışa aktarır.

## Yerel kullanım

Komut satırından çalıştırıldığında Excel dosyası üretir:

```bash
python3 outputs/letterboxd_actors.py kullanici_adi
```

Hesap argümanı verilmezse tarayıcı arayüzü açılır. Arayüz analiz sırasında Excel üretmez; yalnızca kullanıcı `Excel indir` düğmesine bastığında dosyayı hazırlar:

```bash
python3 outputs/letterboxd_actors.py
```

## Docker

```bash
docker build -t letterboxd-actors .
docker run --rm -p 8000:8000 letterboxd-actors
```

Arayüz `http://localhost:8000` adresinde açılır.

## Render'a deploy

Repo kökündeki `render.yaml`, ücretsiz Frankfurt web servisini ve Docker build ayarlarını tanımlar. Render Dashboard'da **New > Blueprint** seçip bu repoyu bağlamak yeterlidir. `main` dalına gönderilen sonraki commitler otomatik deploy edilir.

## Ortam değişkenleri

- `PORT`: Web sunucusunun portu. Varsayılan `8000`.
- `DATABASE_URL`: İsteğe bağlı PostgreSQL bağlantısı. Verilirse cast önbelleği yeniden başlatmalarda korunur.
- `LETTERBOXD_HOSTED=1`: Sunucuyu `0.0.0.0` üzerinde başlatır ve yerel kapatma kontrolünü gizler.
- `LETTERBOXD_XLSX_BACKEND=xlsxwriter`: Taşınabilir Excel motorunu kullanır; Docker imajında varsayılandır.

## Sınırlar

Yalnızca herkese açık profiller okunabilir. Araç Letterboxd'ın herkese açık HTML sayfalarını kullandığı için site yapısındaki değişiklikler ayrıştırıcıyı etkileyebilir. Küçük ücretsiz instance'ı korumak için aynı anda tek analiz çalışır; tamamlanan sonuçlar sunucu belleğinde yaklaşık 30 dakika tutulur.
