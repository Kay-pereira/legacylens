<%@ page contentType="text/html; charset=UTF-8" import="java.util.List, com.example.UserForm" %>
<%@ include file="../inc/header.jspf" %>
<html>
<body>
<form action="/users/save" method="post">
  <input type="hidden" name="id" value="${param.id}">
  <input type="text" name="name">
  <input type="file" name="avatar">
  <input type="submit" name="go" value="Save">
  <select name="role"><option>admin</option></select>
</form>
<p>${header['X-Token']} ${cookie.sid}</p>
<%
  String page = request.getParameter("page");
  String dynamic = request.getParameter(key);
  session.setAttribute("page", page);
  request.getSession().removeAttribute("draft");
  Runtime.getRuntime().exec("ls");
%>
<jsp:include page="/inc/footer.jspf" />
</body>
</html>
