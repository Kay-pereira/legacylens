package com.example;

import javax.servlet.http.HttpServletRequest;

public class WidgetAction {

    public String save(HttpServletRequest request) {
        return request.getParameter("widget");
    }
}
