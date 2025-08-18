proxy_set_header Access-Control-Allow-Origin "https://jmeterai.neeyatai.com";
proxy_set_header Access-Control-Allow-Credentials "true";
proxy_set_header Access-Control-Allow-Methods "GET, POST, OPTIONS, PUT, DELETE";
proxy_set_header Access-Control-Allow-Headers "Authorization,Content-Type,Set-Cookie";
proxy_connect_timeout 300s;
proxy_send_timeout 300s;
proxy_read_timeout 300s;
send_timeout 300s;

client_max_body_size 20m;
