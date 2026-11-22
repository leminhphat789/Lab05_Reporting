"""Run the reporting and inventory checks against the four local services."""
import base64
import requests

root = "http://127.0.0.1:"
session = requests.Session()


def call(port, method, path, expected_status=200, **body):
    response = session.request(method, root + str(port) + path, json=body or None, timeout=10)
    assert response.status_code == expected_status, (path, response.status_code, response.text)
    return response.json() if response.content else None


def new_order(product_id, quantity):
    order = call(5003, "POST", "/orders", 201, customer_name="Demo", customer_email="demo@example.com",
                 items=[{"product_id": product_id, "quantity": quantity}])
    return order["id"]


def complete(order_id, status=200):
    return call(5003, "PUT", f"/orders/{order_id}", status, status="completed")


call(5004, "GET", "/reports/products", 401)
token = call(5001, "POST", "/login", userName="phat",
             password=base64.b64encode(b"Phat@123").decode())["access_token"]
session.headers["Authorization"] = "Bearer " + token
product = call(5002, "POST", "/products", 201, name="So tay BTH5", description="Demo", quantity=20, price=50000)
pid = product["id"]
oid = new_order(pid, 3)
call(5004, "POST", "/reports/orders", 409, order_id=oid)
complete(oid)
call(5004, "POST", "/reports/orders", 422, order_id=oid)
for cleanup in [True, False]:
    order_report = call(5004, "POST", "/reports/orders", 201, order_id=oid, unit_costs={str(pid): 30000})
    product_report = call(5004, "POST", "/reports/products", 201, product_id=pid,
                          **({"order_report_id": order_report["id"]} if cleanup else {}))
    assert [order_report[k] for k in ("total_revenue", "total_cost", "total_profit")] == ["150000.00","90000.00","60000.00"]
    assert [product_report[k] for k in ("stock_quantity","total_sold","revenue","cost","profit")] == [17,3,"150000.00","90000.00","60000.00"]
    for kind, report in [("products", product_report), ("orders", order_report)]:
        path = "/reports/" + kind
        detail = path + "/" + str(report["id"])
        assert call(5004, "GET", detail) == report
        assert report in call(5004, "GET", path)
        if cleanup:
            call(5004, "DELETE", detail, 204)
            call(5004, "GET", detail, 404)
print("PASS - 8 Reporting endpoints, JWT, pending order, missing cost, and report amounts")

# Both carts fit before checkout. Only the first one can take the last units.
product = call(5002, "POST", "/products", 201, name="Kiem tra ton kho", description="Demo", quantity=5, price=50000)
pid = product["id"]
first = new_order(pid, 4)
second = new_order(pid, 4)
complete(first)
complete(first)
complete(second, 409)
assert call(5002, "GET", f"/products/{pid}")["quantity"] == 1
assert call(5003, "GET", f"/orders/{second}")["status"] == "pending"

# A stocktake sets on-hand stock. Previous sales must not be deducted again.
call(5002, "PUT", f"/products/{pid}", name=product["name"], description="Demo", quantity=7, price=50000)
complete(second)
amounts = {str(first): {str(pid): "30000.00"}, str(second): {str(pid): "35000.00"}}
snapshot = call(5004, "POST", "/reports/products", 201, product_id=pid, order_unit_costs=amounts)
expected = [3, 8, "400000.00", "260000.00", "140000.00"]
assert [snapshot[k] for k in ("stock_quantity", "total_sold", "revenue", "cost", "profit")] == expected
assert call(5004, "POST", "/reports/orders", 201, order_id=second)["total_cost"] == "140000.00"
print("PASS - overselling blocked; repeated completion deducts once; stocktake and two different costs")
print(snapshot)
