package com.example;

import java.io.FileWriter;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import javax.mail.Transport;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import javax.servlet.http.HttpSession;
import org.apache.struts.action.Action;
import org.apache.struts.action.ActionForm;
import org.apache.struts.action.ActionForward;
import org.apache.struts.action.ActionMapping;

public class SaveUserAction extends Action {

    public ActionForward execute(
            ActionMapping mapping,
            ActionForm form,
            HttpServletRequest request,
            HttpServletResponse response) {
        String mode = request.getParameter("mode");
        UserForm userForm = (UserForm) form;
        request.setAttribute("mode", mode);
        return mapping.findForward(userForm.getName());
    }

    private boolean save(Connection connection, UserForm form) throws Exception {
        PreparedStatement insert = connection.prepareStatement(
                "INSERT INTO users (name, email) VALUES (?, ?)");
        insert.setString(1, form.getName());
        insert.setString(2, form.getEmail());
        return insert.executeUpdate() > 0;
    }

    private String findEmail(Connection connection, String name) throws Exception {
        PreparedStatement select = connection.prepareStatement(
                "SELECT email FROM users WHERE name = ?");
        select.setString(1, name);
        ResultSet rows = select.executeQuery();
        return rows.next() ? rows.getString("email") : null;
    }

    private void archiveUsers(
            UserForm form, HttpServletRequest request, HttpSession session) throws Exception {
        new FileWriter("reports/users.csv").write(form.getName());
        Transport.send(null);
        Runtime.getRuntime().exec("ls reports");
        new ProcessBuilder("gzip", "reports/users.csv").start();
        session.setAttribute("lastExport", form.getName());
        request.getSession().removeAttribute("draft");
    }
}
