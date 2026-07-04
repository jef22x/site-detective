<!DOCTYPE html>
<html><head><title>Mock WP Login</title></head>
<body>
  <h1>Log In</h1>
  <input id="user_login" placeholder="Username">
  <input id="user_pass" type="password" placeholder="Password">
  <button id="wp-submit" onclick="
    if (document.getElementById('user_login').value === 'admin' &&
        document.getElementById('user_pass').value === 'changeme') {
      location.href = 'admin/orders.html';
    } else {
      document.body.insertAdjacentHTML('beforeend', '<p id=login_error>Invalid credentials</p>');
    }">Log In</button>
</body></html>
