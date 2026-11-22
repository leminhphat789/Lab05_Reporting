# Bài thực hành 5 - Dịch vụ báo cáo

Reporting chạy bằng Flask trên cổng **5004**, tự tạo CSDL SQLite `reporting.sqlite3` khi khởi động. Dịch vụ xác thực JWT qua BTH2, đọc tồn kho từ BTH3 và đọc các đơn đã hoàn tất từ BTH4 qua HTTP.

## Chạy trên Windows

1. Tải hoặc clone dự án. Mở PowerShell trong thư mục chứa `start.ps1`.
2. Chuẩn bị Python 3 và MariaDB (có thể dùng bản đi kèm XAMPP). Cần có mạng để cài thư viện bằng pip lần đầu.
3. Khởi động bốn dịch vụ bằng lệnh sau.

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Script tạo `.venv`, cài `requirements.txt` và chuẩn bị bảng sản phẩm. Mặc định, MariaDB của bài chạy trên `127.0.0.1:3315`, dùng dữ liệu trong `mysql-data`. Nếu script chưa tìm được MariaDB, đặt `$env:BTH5_MYSQLD` bằng đường dẫn đầy đủ tới `mysqld.exe` hoặc `mariadbd.exe` rồi chạy lại.

Nếu dùng MySQL của XAMPP ở cổng 3306, bật MySQL rồi chạy trong PowerShell.

```powershell
$env:DB_HOST = '127.0.0.1'
$env:DB_PORT = '3306'
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

| Dịch vụ | Cổng | Dữ liệu |
|---|---:|---|
| BTH2 - Xác thực | 5001 | SQLite, tài khoản và token |
| BTH3 - Sản phẩm | 5002 | MariaDB, sản phẩm và số lượng tồn kho |
| BTH4 - Đơn hàng | 5003 | SQLite, đơn và chi tiết đơn |
| BTH5 - Reporting | 5004 | SQLite, báo cáo và giá vốn từng mặt hàng theo đơn |

Để chạy riêng Reporting khi các dịch vụ nguồn đã sẵn sàng, dùng `.\.venv\Scripts\python.exe .\app.py`. Kiểm tra dịch vụ ở `http://127.0.0.1:5004/health`.

## Kiểm tra kết quả

- Chạy `.\.venv\Scripts\python.exe .\demo.py`. Script dùng tài khoản mẫu, tạo sản phẩm và đơn mới, kiểm tra đủ 8 API, thử hoàn tất lại đơn, bán quá tồn, kiểm kê và hai mức giá vốn. Dữ liệu mẫu được lưu để có thể xem lại.
- Hoặc import `BTH5.postman_collection.json` vào Postman và chọn **Run collection** theo đúng thứ tự. Collection tự đăng nhập, tạo sản phẩm, tạo đơn, hoàn tất đơn và dùng ID nhận được cho các yêu cầu báo cáo.
- Tài khoản mẫu là `phat`, mật khẩu `Phat@123`. Yêu cầu đăng nhập dùng Base64 theo quy ước của bài 2. Các API báo cáo yêu cầu header `Authorization: Bearer <token>`.

## API báo cáo

| Thao tác | Báo cáo sản phẩm | Báo cáo đơn hàng |
|---|---|---|
| Tạo | `POST /reports/products` | `POST /reports/orders` |
| Danh sách | `GET /reports/products` | `GET /reports/orders` |
| Chi tiết | `GET /reports/products/{id}` | `GET /reports/orders/{id}` |
| Xóa | `DELETE /reports/products/{id}` | `DELETE /reports/orders/{id}` |

Trong URL chi tiết và xóa, `id` là **ID báo cáo** do lệnh POST trả về. Trong nội dung tạo báo cáo, `product_id` và `order_id` là ID từ dịch vụ nguồn. Báo cáo lưu số liệu tại thời điểm tạo. Khi cần số mới, tạo báo cáo mới.

### Báo cáo đơn hàng

Ví dụ đơn 12 có sản phẩm 7, giá vốn một đơn vị là 30.000. Thay các ID bằng ID thực tế.

```json
{"order_id": 12, "unit_costs": {"7": "30000.00"}}
```

### Báo cáo sản phẩm qua nhiều đơn

Ví dụ sản phẩm 7 có giá vốn 30.000 ở đơn 12 và 35.000 ở đơn 13. Có thể gửi trực tiếp tới `POST /reports/products`.

```json
{
  "product_id": 7,
  "order_unit_costs": {
    "12": {"7": "30000.00"},
    "13": {"7": "35000.00"}
  }
}
```

Giá vốn được lưu theo cặp đơn hàng và sản phẩm. Khi đã ghi đủ giá vốn, chỉ cần gửi `{"product_id": 7}`. Để giới hạn số bán và số tiền vào một đơn cụ thể, thêm `order_report_id` là ID báo cáo đơn hàng đã tạo. Tồn kho luôn thể hiện số tồn hiện tại của sản phẩm.

Khi sản phẩm đã có giá vốn ở các đơn cũ và có thêm đơn mới, gửi `order_unit_costs` cho từng đơn còn thiếu. API trả `422` nếu thiếu giá vốn và `409` khi yêu cầu ghi đè giá vốn đã lưu.

## Cách tính báo cáo

- Chỉ tính số bán và doanh thu từ đơn `completed`. Doanh thu dùng thành tiền lưu trên từng dòng đơn. Chi phí bằng tổng số lượng nhân giá vốn của từng dòng. Lợi nhuận bằng doanh thu trừ chi phí.
- Dùng các phiên bản BTH3 và BTH4 đi kèm gói này. BTH3 giữ số tồn hiện tại. Khi hoàn tất đơn, BTH4 yêu cầu BTH3 trừ các mặt hàng đúng một lần. Cập nhật `quantity` ở BTH3 khi kiểm kê là đặt lại số lượng tồn kho.
